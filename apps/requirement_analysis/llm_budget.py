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
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_PAID_HINTS = ("max", "plus", "pro", "2.4t")
_RETAIN_DAYS = 14

# 免费额度耗尽（用完即停生效）的官方错误码——**唯一权威的「该模型免费额度已耗尽」信号**。
FREE_TIER_ONLY_CODE = "AllocationQuota.FreeTierOnly"

# 临近到期闸值（分钟）：剩余时长小于该值即提前禁用，避免调用跨过到期时刻产生费用。
DEFAULT_EXPIRY_GUARD_MINUTES = 60

_LOCK = threading.Lock()

# 文本对话链路可用的模型类型（可用于 chat/completions 文本生成）。
# 视觉/视频/音频类模型不能拿到文本对话链路里跑。
# multimodal/omni：多模态理解型支持「文本+图/音/视频输入 → 文本输出」，可用于文本链路。
TEXT_CAPABLE_TYPES = {"llm", "text", "chat", "language", "nlp", "multimodal", "omni"}

# 明确不可用于文本链路的类型（给出可读原因，避免只报「非文本」让人猜）。
NON_TEXT_TYPES = {
    "image",
    "vision",
    "video",
    "audio",
    "embedding",
    "rerank",
    "realtime",
}


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


_TRUE_VALUES = ("1", "true", "yes", "on", "y", "t")
_FALSE_VALUES = ("0", "off", "false", "no", "n", "f")


def _env(key: str) -> str | None:
    """读配置：先真实环境变量，再退到项目 `.env`。

    为什么不能只用 `os.getenv`：Django 是用 `python-decouple` 读 `.env` 的，它**不会**
    把值写进 `os.environ`。于是 `LLM_MODEL_REGISTRY` / `LLM_MODEL_ALLOWLIST` 这类护栏
    配置在 Django 进程里永远读不到 —— 表现为「登记册明明写了 53 条，运行时却一条不生效，
    过期模型照跑」。真实环境变量优先，保证 CI/生产注入变量时不被 `.env` 覆盖。
    """
    val = os.getenv(key)
    if val is not None:
        return val
    try:
        from decouple import config as _cfg
    except ImportError:
        return None
    try:
        return _cfg(key, default=None)
    except Exception:  # noqa: BLE001 - 配置读不到等价于未配置，交给调用方的默认值
        return None


def _as_bool(raw: str | None, default: bool) -> bool:
    """布尔解析。`0/off/no/false` 必须解析为 False——否则 `LLM_BUDGET_ENFORCE=0`
    会被当成默认值 True，护栏就变成「无法关闭」且与 sidecar 侧行为不一致。
    """
    if raw is None or str(raw).strip() == "":
        return default
    val = str(raw).strip().lower()
    if val in _TRUE_VALUES:
        return True
    if val in _FALSE_VALUES:
        return False
    return default


def _as_int(raw: str | None, default: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _default_usage_path() -> str:
    env = _env("LLM_USAGE_PATH")
    if env:
        return env
    base = _env("DJANGO_BASE_DIR") or os.getcwd()
    return str(Path(base) / "data" / "llm_usage.json")


def load_settings() -> BudgetSettings:
    """读取护栏配置（每次现读，便于测试与运行期热调）。"""
    return BudgetSettings(
        enforce=_as_bool(_env("LLM_BUDGET_ENFORCE"), True),
        allowlist=[m.lower() for m in _split_csv(_env("LLM_MODEL_ALLOWLIST"))],
        paid_hints=[h.lower() for h in _split_csv(_env("LLM_PAID_MODEL_HINTS"))]
        or list(DEFAULT_PAID_HINTS),
        allow_paid=_as_bool(_env("LLM_ALLOW_PAID_MODEL"), False),
        max_input_tokens=_as_int(_env("LLM_CALL_MAX_INPUT_TOKENS"), 24000),
        max_output_tokens=_as_int(_env("LLM_CALL_MAX_OUTPUT_TOKENS"), 4096),
        daily_token_budget=_as_int(_env("LLM_DAILY_TOKEN_BUDGET"), 300000),
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
        return (
            ""
            if name in cfg.allowlist
            else f"模型 {model} 不在 LLM_MODEL_ALLOWLIST 白名单内"
        )
    # 白名单为空：登记在册的「确认用完即停」免费模型，越过付费关键字子串误判
    # （如 deepseek-v4-pro 含 'pro' 但实为免费档）；类型/到期/开启校验交给 check_model_registry。
    if _registry_has(name) or cfg.allow_paid:
        return ""
    hit = next((h for h in cfg.paid_hints if h and h in name), "")
    return (
        f"模型 {model} 命中付费关键字 '{hit}'（免费额度耗尽会静默转按量计费）"
        if hit
        else ""
    )


# ---------------------------------------------------------------------------
# 模型登记册（LLM_MODEL_REGISTRY）：把截图里的「模型|类型|到期|是否开启」变成
# 机器可校验的元数据，落地「判断类型是否可用 / 是否到期 / 是否开启」三道闸。
# 格式：每条 `name|type|expiry|enabled`，多条逗号分隔。增强型校验——不在册的模型不强制。
# ---------------------------------------------------------------------------

# 到期时间支持的写法：YYYY-MM-DD（当天 23:59:59 失效）/ YYYY-MM-DD HH:MM[:SS] /
# YYYY-MM-DDTHH:MM[:SS][.fff][Z] / 斜杠日期。解析不出 → None。
# **不做「截前 10 字符」兜底**：那会把 "2026-11-13 25:99" 静默当成 11-13 放行（fail-open）。
_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S.%f",
)
_DATE_FORMATS = ("%Y-%m-%d",)

# 时区偏移后缀（如 +08:00）：登记册是人工配置的本地时间，偏移只剥离不换算
_TZ_OFFSET_SUFFIX = re.compile(r"[+-]\d{2}:?\d{2}$")


def _parse_expire_at(raw: str | None) -> datetime | None:
    """解析到期时间 → datetime。只写日期时按该日 23:59:59 失效；解析不出返回 None。"""
    s = (raw or "").strip().replace("/", "-")
    if not s:
        return None
    if s.endswith(("Z", "z")):
        s = s[:-1]
    s = _TZ_OFFSET_SUFFIX.sub("", s).strip()
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(hour=23, minute=59, second=59)
        except ValueError:
            continue
    return None


def _parse_expiry(raw: str) -> date | None:
    """兼容旧接口：返回到期**日期**。解析不出返回 None。"""
    at = _parse_expire_at(raw)
    return at.date() if at else None


def expiry_guard_minutes() -> int:
    """临近到期闸值（分钟）= `LLM_EXPIRY_GUARD_MINUTES`，默认 60。**0 = 关闭该闸**。"""
    return max(
        0, _as_int0(_env("LLM_EXPIRY_GUARD_MINUTES"), DEFAULT_EXPIRY_GUARD_MINUTES)
    )


@dataclass
class ExpiryState:
    """单个模型的到期状态（时刻精度）。`blocked`= 剩余时长小于闸值（含已过期）。"""

    expire_at: datetime | None = None
    expired: bool = False
    invalid: bool = False
    assumed: bool = False
    seconds_left: float | None = None
    blocked: bool = False
    reason: str = ""


def _parse_registry(raw: str | None) -> dict[str, dict[str, Any]]:
    """解析 LLM_MODEL_REGISTRY → {name_lower: {type, expiry, enabled}}。

    同名重复时**开启优先、先声明优先**：换批次录入时很容易出现「旧的置 off 条目
    还留在后面」的情况，若简单地后写覆盖，一条已作废的 off 会把新录入的 on 顶掉，
    表现为「刚录入的模型却不可用」。这里显式去重并告警，不靠书写顺序。
    """
    reg: dict[str, dict[str, Any]] = {}
    if not raw:
        return reg
    duplicated: list[str] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        fields = [f.strip() for f in part.split("|")]
        name = fields[0].lower()
        if not name:
            continue
        mtype = (fields[1].lower() if len(fields) > 1 else "") or "llm"
        expiry = fields[2].strip() if len(fields) > 2 else ""
        enabled_raw = (fields[3].lower() if len(fields) > 3 else "") or "on"
        enabled = enabled_raw not in ("off", "0", "false", "no", "disabled")
        entry = {"type": mtype, "expiry": expiry, "enabled": enabled}
        prev = reg.get(name)
        if prev is None:
            reg[name] = entry
            continue
        duplicated.append(name)
        if enabled and not prev["enabled"]:
            reg[name] = entry
    if duplicated:
        logger.warning(
            "LLM_MODEL_REGISTRY 有重复模型名 %s，已按「开启优先/先声明优先」去重",
            sorted(set(duplicated)),
        )
    return reg


def _registry_has(name: str) -> bool:
    """模型是否已登记在 LLM_MODEL_REGISTRY（即「已确认开了用完即停」）。"""
    raw = _env("LLM_MODEL_REGISTRY")
    if not raw:
        return False
    return name in _parse_registry(raw)


# ---------------------------------------------------------------------------
# 免费额度耗尽标记（FreeTierOnly 一等信号）
#
# 百炼 API 不返回「本次调用是否免费」——调用成功既可能在免费额度内，也可能已在按量计费。
# 平台唯一权威信号是「额度耗尽且已开启用完即停」时的 HTTP 403 + AllocationQuota.FreeTierOnly。
# 把该信号落盘成一等状态：命中一次即标记，之后调用前本地直接跳过（可降级到下一个），
# 对外暴露三态：exhausted / ok / unknown。
# ---------------------------------------------------------------------------


def _quota_state_path() -> Path:
    raw = _env("LLM_QUOTA_STATE_PATH")
    if raw:
        return Path(raw)
    base = _env("DJANGO_BASE_DIR") or os.getcwd()
    return Path(base) / "data" / "llm_quota_state.json"


def _as_int0(raw: str | None, default: int) -> int:
    """允许 0 的整数解析（`_as_int` 会把 0 当非法并回退默认值）。"""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _quota_ttl_hours() -> int:
    """标记有效期（小时）。0（默认）= 永久：免费额度按模型固定，耗尽后不会自行恢复。"""
    return _as_int0(_env("LLM_QUOTA_MARKER_TTL_HOURS"), 0)


def _quota_marks() -> dict[str, dict[str, Any]]:
    data = _read_usage(str(_quota_state_path()))
    marks = data.get("models")
    return marks if isinstance(marks, dict) else {}


def _quota_mark(model: str) -> dict[str, Any] | None:
    """读取单个模型的标记（超出 TTL 的视作不存在）。"""
    mark = _quota_marks().get((model or "").strip().lower())
    if not isinstance(mark, dict):
        return None
    ttl = _quota_ttl_hours()
    if ttl > 0:
        at = _parse_expire_at(str(mark.get("at") or ""))
        if at is None or (datetime.now() - at).total_seconds() > ttl * 3600:
            return None
    return mark


def is_free_tier_exhausted(exc: Exception) -> bool:
    """异常是否为「免费额度耗尽（用完即停生效）」——平台唯一权威的额度耗尽信号。

    不把 429 / 普通 quota 误判为耗尽：那类是可恢复的限流，标成永久耗尽会误杀好模型。
    同时兼容 httpx（`response.status_code` / `response.text`）与 openai SDK 异常形态。
    """
    resp = getattr(exc, "response", None)
    status = getattr(exc, "status_code", None)
    if status is None and resp is not None:
        status = getattr(resp, "status_code", None)
    text = " ".join(
        str(part or "")
        for part in (
            getattr(exc, "message", ""),
            getattr(exc, "code", ""),
            getattr(exc, "body", ""),
            getattr(resp, "text", "") if resp is not None else "",
            exc,
        )
    )
    if FREE_TIER_ONLY_CODE in text:
        return True
    if status != 403:
        return False
    low = text.lower()
    return "free quota" in low or "free tier only" in low


def mark_quota_exhausted(
    model: str,
    *,
    reason: str = FREE_TIER_ONLY_CODE,
    message: str = "",
    source: str = "runtime",
) -> bool:
    """落盘标记「该模型免费额度已耗尽」。返回是否标记（模型名为空则 False）。"""
    key = (model or "").strip().lower()
    if not key:
        return False
    path = str(_quota_state_path())
    with _LOCK:
        data = _read_usage(path)
        marks = data.get("models")
        if not isinstance(marks, dict):
            marks = {}
        marks[key] = {
            "state": "exhausted",
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "reason": reason,
            "message": (message or "")[:300],
            "source": source,
        }
        data["models"] = marks
        _write_usage(path, data)
    logger.warning(
        "模型 %s 免费额度已耗尽（%s），已落盘标记，后续调用直接跳过", model, reason
    )
    return True


def clear_quota_marks(model: str | None = None) -> int:
    """清除额度标记。`model` 为空=清全部（换新一批免费模型时用）。返回清除条数。"""
    path = str(_quota_state_path())
    with _LOCK:
        data = _read_usage(path)
        marks = data.get("models")
        if not isinstance(marks, dict) or not marks:
            return 0
        if model is None:
            removed = len(marks)
            data["models"] = {}
        else:
            key = (model or "").strip().lower()
            removed = 1 if key in marks else 0
            marks.pop(key, None)
            data["models"] = marks
        _write_usage(path, data)
    if removed:
        logger.info("已清除 %d 条免费额度耗尽标记（%s）", removed, model or "全部")
    return removed


def _seen_success(model: str, cfg: BudgetSettings) -> bool:
    """该模型是否成功调用过（依据用量账本 by_model，零额外写盘）。"""
    target = (model or "").strip().lower()
    for entry in _read_usage(cfg.usage_path).values():
        if not isinstance(entry, dict):
            continue
        by_model = entry.get("by_model")
        if not isinstance(by_model, dict):
            continue
        for key in by_model:
            if str(key).split("@")[0].strip().lower() == target:
                return True
    return False


def quota_status(model: str, cfg: BudgetSettings | None = None) -> str:
    """该模型免费额度状态三态：`exhausted` / `ok` / `unknown`。

    `ok` 只代表「有过成功调用且未收到耗尽信号」，**不是平台给的余额证明**。
    """
    cfg = cfg or load_settings()
    mark = _quota_mark(model)
    if mark and mark.get("state") == "exhausted":
        return "exhausted"
    return "ok" if _seen_success(model, cfg) else "unknown"


# 「长期 / 到期留空」的默认有效期（天）。约定：留空按一个月算，一个月后自动失效，
# 避免「截图没写到期 → 被当成永久可用 → 过了期还在调 → 静默转按量计费」。
DEFAULT_EXPIRY_DAYS = 30


def _default_expiry_days() -> int:
    return _as_int(_env("LLM_MODEL_DEFAULT_EXPIRY_DAYS"), DEFAULT_EXPIRY_DAYS)


def _baseline_path() -> Path:
    raw = _env("LLM_REGISTRY_BASELINE_PATH")
    if raw:
        return Path(raw)
    base = _env("DJANGO_BASE_DIR") or os.getcwd()
    return Path(base) / "data" / "llm_registry_baseline.json"


def _registry_snapshot_date() -> date | None:
    """截图登记日（`LLM_MODEL_REGISTRY_SNAPSHOT`）：这批模型的「长期」从这天起算。

    语义：**用户给截图、模型登记进配置的那一天**，不是每天滚动。
    """
    raw = (_env("LLM_MODEL_REGISTRY_SNAPSHOT") or "").strip()
    if not raw:
        return None
    parsed = _parse_expiry(raw)
    if parsed is None:
        logger.warning(
            "LLM_MODEL_REGISTRY_SNAPSHOT=%r 无法解析（应写 YYYY-MM-DD），改用按模型首次登记日推算",
            raw,
        )
    return parsed


def _baseline_for(name: str) -> date | None:
    """「长期」模型的基准日：**登记日 + N 天，之后固定不动**；无法确定时返回 None。

    优先级：显式登记日 > 该模型首次被登记到的日期（落盘）。
    绝不能「每次 today + N 天」——那样永远差 N 天、永不过期。
    **落盘失败必须返回 None（fail-closed）**：退回 today 会让「长期」永远差 30 天，等于永不过期。
    """
    snapshot = _registry_snapshot_date()
    if snapshot is not None:
        return snapshot
    key = (name or "").strip().lower()
    path = str(_baseline_path())
    with _LOCK:
        data = _read_usage(path)
        raw = str(data.get(key) or "")
        if raw:
            try:
                return datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                logger.warning("登记册基准日损坏（%s=%r），按今天重置", key, raw)
        today = date.today()
        data[key] = today.isoformat()
        _write_usage(path, data)
        if str(_read_usage(path).get(key) or "") != today.isoformat():
            logger.error(
                "登记册基准日落盘失败（%s）：无法确定「长期」的起算日，按 fail-closed 拒绝该模型；"
                "请显式配置 LLM_MODEL_REGISTRY_SNAPSHOT 或把到期日写全",
                path,
            )
            return None
    return today


def resolve_expire_at(
    name: str, raw_expiry: str, *, now: datetime | None = None
) -> ExpiryState:
    """解析某模型的实际**截止时刻**并判定是否应禁用。

    - 只写日期：按该日 23:59:59 失效（当天整天可用）；
    - 写了时刻：精确判定；
    - 解析不出：invalid → 调用方 fail-closed 拒绝；
    - 留空：按「登记日 + N 天（默认 30）」的当日 23:59:59 推定；
    - **剩余时长小于闸值（默认 60 分钟）即 blocked**：提前禁用，避免调用跨过到期时刻。
    """
    now = now or datetime.now()
    raw = (raw_expiry or "").strip()
    assumed = False
    if not raw:
        base = _baseline_for(name)
        if (
            base is None
        ):  # 基准日无法确定 → fail-closed，绝不退回「今天 + N 天」（那等于永不过期）
            return ExpiryState(
                invalid=True,
                reason="「长期」起算日无法确定（基准日落盘失败，需配置 LLM_MODEL_REGISTRY_SNAPSHOT）",
            )
        expire_at = datetime.combine(
            base + timedelta(days=_default_expiry_days()), dtime(23, 59, 59)
        )
        assumed = True
    else:
        expire_at = _parse_expire_at(raw)
        if expire_at is None:
            return ExpiryState(invalid=True, reason=f"到期时间 '{raw_expiry}' 无法解析")
    seconds_left = (expire_at - now).total_seconds()
    expired = seconds_left <= 0
    blocked = expired or seconds_left < expiry_guard_minutes() * 60
    return ExpiryState(
        expire_at=expire_at,
        expired=expired,
        assumed=assumed,
        seconds_left=seconds_left,
        blocked=blocked,
    )


def resolve_expiry(name: str, raw_expiry: str) -> tuple[date | None, bool, bool, bool]:
    """兼容旧接口：(截止日, 已过期, 日期非法, 是否按默认推定)。"""
    st = resolve_expire_at(name, raw_expiry)
    return (
        st.expire_at.date() if st.expire_at else None,
        st.expired,
        st.invalid,
        st.assumed,
    )


def check_model_registry(model: str, cfg: BudgetSettings | None = None) -> str:
    """按登记册校验类型/开启/到期。不在册的模型放行（交给 allowlist/付费关键字）。"""
    raw = _env("LLM_MODEL_REGISTRY")
    if not raw:
        return ""
    entry = _parse_registry(raw).get((model or "").strip().lower())
    if not entry:
        return ""
    return _registry_reject_reason(model, entry)


def _fmt_moment(at: datetime | None) -> str:
    return at.strftime("%Y-%m-%d %H:%M:%S") if at else "未知时刻"


def _type_reject_reason(model: str, mtype: str) -> str:
    """类型闸：非文本对话模型 → 拒绝原因；可用则空串。"""
    if mtype in TEXT_CAPABLE_TYPES:
        return ""
    if mtype == "realtime":
        return (
            f"模型 {model} 类型='realtime' 不可用：实时多模态走 websocket 实时接口，"
            f"不兼容 chat/completions 文本链路"
        )
    return (
        f"模型 {model} 类型='{mtype}' 不可用：非文本对话模型"
        f"（视觉/视频/音频类不能用于文本生成链路）"
    )


def _expiry_reject_reason(model: str, entry: dict[str, Any]) -> str:
    """到期闸 + 临近到期闸 → 拒绝原因；可用则空串。"""
    st = resolve_expire_at(model, entry["expiry"])
    if st.invalid:
        if not (entry["expiry"] or "").strip():
            return (
                f"模型 {model} 未登记到期日且「长期」起算日无法确定（基准日落盘失败），"
                f"按 fail-closed 拒绝；请配置 LLM_MODEL_REGISTRY_SNAPSHOT 或写全到期日"
            )
        return (
            f"模型 {model} 到期时间 '{entry['expiry']}' 无法解析（请写 YYYY-MM-DD 或 "
            f"YYYY-MM-DD HH:MM），按 fail-closed 拒绝以免过期后继续调用"
        )
    if not st.blocked:
        return ""
    when = _fmt_moment(st.expire_at)
    if st.expired:
        if st.assumed:
            return (
                f"模型 {model} 未登记到期日，按默认 {_default_expiry_days()} 天有效期推定，"
                f"已于 {when} 到期（自动失效）"
            )
        return f"模型 {model} 免费额度已于 {when} 到期，不参与调用"
    remain = max(1, int((st.seconds_left or 0) // 60))
    return (
        f"模型 {model} 距到期仅剩 {remain} 分钟（<{expiry_guard_minutes()} 分钟提前禁用闸），"
        f"将于 {when} 失效，已提前禁用以免调用跨过到期时刻产生费用"
    )


def _registry_reject_reason(model: str, entry: dict[str, Any]) -> str:
    """登记册四项校验（开启 / 类型 / 到期 / 临近到期闸）→ 拒绝原因，空串=通过。"""
    if not entry["enabled"]:
        return f"模型 {model} 已关闭（用完即停 开关=off），不参与调用"
    return _type_reject_reason(model, entry["type"]) or _expiry_reject_reason(
        model, entry
    )


def _remaining_text(minutes_left: int | None, days_left: int | None) -> str:
    """剩余时间提示（未触发禁用闸时的友好说明）；无需提示返回空串。"""
    if days_left is not None and days_left <= 0 and minutes_left is not None:
        return f"剩余 {minutes_left // 60} 小时 {minutes_left % 60} 分（当日到期）"
    if days_left is not None and days_left <= 7:
        return f"剩余 {days_left} 天（即将到期）"
    return ""


def _expiry_verdict_reason(entry: dict[str, Any], st: ExpiryState) -> str:
    """到期类的展示原因；无问题返回空串。"""
    if st.invalid:
        return (
            "「长期」起算日无法确定（基准日落盘失败，按拒绝处理）"
            if not (entry["expiry"] or "").strip()
            else f"到期时间 '{entry['expiry']}' 无法解析（按拒绝处理）"
        )
    if st.expired:
        when = _fmt_moment(st.expire_at)
        return (
            f"默认 {_default_expiry_days()} 天有效期已于 {when} 到期"
            if st.assumed
            else f"已于 {when} 到期"
        )
    minutes_left = int(st.seconds_left // 60) if st.seconds_left is not None else None
    if st.blocked:
        return (
            f"距到期仅剩 {max(1, minutes_left or 0)} 分钟"
            f"（<{expiry_guard_minutes()} 分钟提前禁用闸）"
        )
    if st.assumed and st.expire_at is not None:
        return (
            f"未写到期→按默认 {_default_expiry_days()} 天推定"
            f"（{_fmt_moment(st.expire_at)} 失效）"
        )
    days_left = (st.expire_at.date() - date.today()).days if st.expire_at else None
    return _remaining_text(minutes_left, days_left)


def _verdict_reasons(
    model: str, entry: dict[str, Any], st: ExpiryState, qstate: str
) -> list[str]:
    """汇总某模型的不可用原因（供判定清单展示）。全部通过则返回空列表。"""
    reasons: list[str] = []
    mtype = entry["type"]
    if mtype not in TEXT_CAPABLE_TYPES:
        reasons.append(
            "类型='realtime' 实时接口，不兼容 chat/completions"
            if mtype == "realtime"
            else f"类型='{mtype}' 非文本对话模型"
        )
    if not entry["enabled"]:
        reasons.append("开关=off")
    expiry_reason = _expiry_verdict_reason(entry, st)
    if expiry_reason:
        reasons.append(expiry_reason)
    if qstate == "exhausted":
        mark = _quota_mark(model) or {}
        reasons.append(
            f"免费额度已耗尽（{mark.get('reason') or FREE_TIER_ONLY_CODE}，"
            f"{mark.get('at') or '时间未知'} 标记）"
        )
    return reasons


def _expire_sort_key(row: dict[str, Any]) -> datetime:
    """排序键：有到期时刻的按其升序，无 / 非法的排最后。"""
    return _parse_expire_at(str(row.get("expire_at") or "")) or datetime.max


def evaluate_registry(cfg: BudgetSettings | None = None) -> list[dict[str, Any]]:
    """对登记册每个模型给出完整判定（类型 / 开启 / 到期 / 临近到期 / 额度状态）。

    排序：先可用的、再不可用的；同组内按**到期时刻升序**（用完即停：先消耗快过期的）。
    """
    cfg = cfg or load_settings()
    reg = _parse_registry(_env("LLM_MODEL_REGISTRY"))
    now = datetime.now()
    today = now.date()
    out: list[dict[str, Any]] = []
    for name, e in reg.items():
        text_ok = e["type"] in TEXT_CAPABLE_TYPES
        enabled = e["enabled"]
        st = resolve_expire_at(name, e["expiry"], now=now)
        expire_at = st.expire_at
        days_left = (expire_at.date() - today).days if expire_at else None
        minutes_left = (
            int(st.seconds_left // 60) if st.seconds_left is not None else None
        )
        qstate = quota_status(name, cfg)
        usable = (
            text_ok
            and enabled
            and not st.invalid
            and not st.blocked
            and qstate != "exhausted"
        )
        reasons = _verdict_reasons(name, e, st, qstate)
        date_iso = expire_at.date().isoformat() if expire_at else ""
        out.append(
            {
                "model": name,
                "type": e["type"],
                # expiry / expiry_date 保持「日期」语义，向后兼容判定脚本与既有测试
                "expiry": date_iso or (e["expiry"] or "长期"),
                "expiry_date": date_iso,
                "expire_at": expire_at.strftime("%Y-%m-%d %H:%M:%S")
                if expire_at
                else "",
                "expiry_assumed": st.assumed,
                "days_left": days_left,
                "minutes_left": minutes_left,
                "enabled": enabled,
                "text_capable": text_ok,
                "expired": bool(st.expired),
                "expiry_invalid": bool(st.invalid),
                "near_expiry": bool(st.blocked and not st.expired),
                "quota_state": qstate,
                "usable_for_text": usable,
                "reason": "；".join(reasons) or "可用",
            }
        )
    out.sort(key=lambda x: (not x["usable_for_text"], _expire_sort_key(x)))
    return out


def sort_chain_by_expiry(chain: list[str]) -> list[str]:
    """把降级链按**到期时刻升序**重排（用完即停：先消耗快过期的）。

    已登记的按 expire_at 升序排前；未登记 / 无到期的排后并保持原有相对顺序。
    """
    if not chain:
        return []
    reg = _parse_registry(_env("LLM_MODEL_REGISTRY"))
    now = datetime.now()
    keyed: list[tuple[bool, datetime, int, str]] = []
    for idx, raw in enumerate(chain):
        name = (raw or "").strip()
        if not name:
            continue
        entry = reg.get(name.lower())
        expire_at = (
            resolve_expire_at(name.lower(), entry["expiry"], now=now).expire_at
            if entry
            else None
        )
        keyed.append((expire_at is None, expire_at or datetime.max, idx, name))
    keyed.sort(key=lambda x: (x[0], x[1], x[2]))
    return [x[3] for x in keyed]


def resolve_text_chain(cfg: BudgetSettings | None = None) -> list[str]:
    """未显式配置 LLM_MODEL_CHAIN 时，从登记册自动推导文本链路（到期时刻升序）。"""
    cfg = cfg or load_settings()
    return [v["model"] for v in evaluate_registry(cfg) if v["usable_for_text"]]


def quota_reject_reason(model: str) -> str:
    """免费额度耗尽标记 → 拒绝原因；无标记返回空串。

    抽成公开函数是为了让**不经过 check_call** 的调用方（AI 用例 browser-use、
    judge 引擎等）复用同一条判定，避免出现「主链路拦、旁路照跑」的漏口。
    """
    mark = _quota_mark(model) or {}
    if mark.get("state") != "exhausted":
        return ""
    return (
        f"模型 {model} 免费额度已耗尽"
        f"（{mark.get('reason') or FREE_TIER_ONLY_CODE}，{mark.get('at') or '时间未知'} 标记），"
        f"跳过并降级"
    )


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

    reason = check_model_registry(model, cfg)
    if reason:
        return BudgetDecision(False, reason, model, est, scope="model")

    # 免费额度已耗尽（由 403 FreeTierOnly 落盘标记）：本地直接跳过，不必每次撞墙
    reason = quota_reject_reason(model)
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
        "expiry_guard_minutes": expiry_guard_minutes(),
        "quota_marks": _quota_marks(),
        "registry": evaluate_registry(cfg),
    }


def reset_today(cfg: BudgetSettings | None = None) -> None:
    """清零当日计数（人工解封用）。"""
    cfg = cfg or load_settings()
    with _LOCK:
        data = _read_usage(cfg.usage_path)
        data.pop(_day_key(), None)
        _write_usage(cfg.usage_path, data)
