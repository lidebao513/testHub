"""引擎 · LLM 成本护栏（core/llm_budget）。

背景：百炼免费额度**耗尽后会自动转按量计费，不报错、不中断**。一旦配置里出现付费档
模型（如 `qwen3.8-max`，12~36 元/百万 token），批量 describe 会在无人察觉的情况下产生
账单。本模块在**唯一 LLM 出口** `engine/llm_fallback.chat_with_fallback` 上设三道闸：

1. **模型门禁**：配置了 `LLM_MODEL_ALLOWLIST` 时只允许清单内模型（最严）；
   未配置时用 `LLM_PAID_MODEL_HINTS` 关键字拦付费档（max/plus/pro/2.4t）。
2. **单次输入预算**：估算输入 token 超 `LLM_CALL_MAX_INPUT_TOKENS` 直接拒绝。
   不做静默截断——截断会悄悄改变语义，让结果看起来正常却不对。
3. **日累计熔断**：当日累计 token 超 `LLM_DAILY_TOKEN_BUDGET` 后拒绝一切调用，
   直到次日或人工重置。

**fail-closed**：这里是钱，判定不确定时一律拒绝并在日志说明原因，不做「拿不准就放行」。
计数持久化到 `data/llm_usage.json`，进程重启不清零（否则重启即可绕过预算）。
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any

from core.enums import QuotaState
from core.errors import LLMError
from core.log import get_logger


log = get_logger(__name__)

# 付费档模型关键字：命中即视为会产生费用（除非显式放行或在 allowlist 内）
DEFAULT_PAID_HINTS = ("max", "plus", "pro", "2.4t")

# 文本对话链路可用的模型类型（可用于 chat/completions 文本生成）。
# 视觉/视频/音频类模型不能拿到文本对话链路里跑，否则要么报错、要么产出图/视频而非文字。
# multimodal/omni：多模态理解型（如 qwen-omni）支持「文本+图/音/视频输入 → 文本输出」，
#   走 OpenAI 兼容 chat/completions 可用，故计入文本可用。
TEXT_CAPABLE_TYPES = {"llm", "text", "chat", "language", "nlp", "multimodal", "omni"}

# 明确不可用于文本链路的类型（给出可读原因，避免只报「非文本」让人猜）。
NON_TEXT_TYPES = {"image", "vision", "video", "audio", "embedding", "rerank", "realtime"}

# 用量文件保留天数（够看一周趋势又不无限增长）
_RETAIN_DAYS = 14

# 免费额度耗尽（用完即停生效）的官方错误码——**唯一权威的「该模型免费额度已耗尽」信号**。
# 平台在免费额度耗尽且开启「用完即停」时返回 HTTP 403 且 code/type 均为该值。
FREE_TIER_ONLY_CODE = "AllocationQuota.FreeTierOnly"

# 临近到期闸值（分钟）：剩余时长小于该值即**提前禁用**该模型。
# 目的：避免一次调用跨过到期时刻——跨期的那部分按量计费不会报错，只会悄悄进账单。
DEFAULT_EXPIRY_GUARD_MINUTES = 60

_LOCK = threading.Lock()


class BudgetExceeded(LLMError):
    """超出成本预算——调用被主动拒绝，不是上游错误。"""


@dataclass
class BudgetDecision:
    """一次调用的准入结论。

    `scope` 决定调用方如何处理拒绝：
    - `model`：仅该模型不可用（如命中付费关键字）→ 可继续试链上下一个模型；
    - `global`：预算类拒绝（输入过大 / 日累计到顶）→ 换模型也一样超，必须整链停止。
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
    """从环境变量解析出的护栏参数（每次调用现读，便于测试与热调）。"""

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


def _as_bool(raw: str | None, default: bool) -> bool:
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


def load_settings() -> BudgetSettings:
    """读取护栏配置。usage 文件默认落在 `data/llm_usage.json`。"""
    root = Path(__file__).resolve().parent.parent
    default_usage = str(root / "data" / "llm_usage.json")
    return BudgetSettings(
        enforce=_as_bool(os.getenv("LLM_BUDGET_ENFORCE"), True),
        allowlist=[m.lower() for m in _split_csv(os.getenv("LLM_MODEL_ALLOWLIST"))],
        paid_hints=[h.lower() for h in _split_csv(os.getenv("LLM_PAID_MODEL_HINTS"))]
        or list(DEFAULT_PAID_HINTS),
        allow_paid=_as_bool(os.getenv("LLM_ALLOW_PAID_MODEL"), False),
        max_input_tokens=_as_int(os.getenv("LLM_CALL_MAX_INPUT_TOKENS"), 24000),
        max_output_tokens=_as_int(os.getenv("LLM_CALL_MAX_OUTPUT_TOKENS"), 4096),
        daily_token_budget=_as_int(os.getenv("LLM_DAILY_TOKEN_BUDGET"), 300000),
        usage_path=os.getenv("LLM_USAGE_PATH") or default_usage,
    )


def estimate_tokens(text: str) -> int:
    """粗估 token 数（不引入 tiktoken 依赖）。

    保守取「1 token ≈ 1.6 字符」：中文偏密（1 字≈1 token）、英文偏疏（4 字符≈1 token），
    1.6 对中英混排是偏高的估计——**宁可高估触发拦截，也不低估放行**。
    """
    if not text:
        return 0
    return max(1, int(len(text) / 1.6))


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """估算消息体总 token（含 role 等结构开销的粗略补偿）。"""
    total = 0
    for msg in messages or []:
        content = msg.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    # 图片按固定成本折算，避免多模态消息被估成 0
                    if part.get("type") == "image_url":
                        total += 1000
                    else:
                        total += estimate_tokens(str(part.get("text", "")))
        total += 4  # 每条消息的结构开销
    return total


def _read_usage(path: str) -> dict[str, Any]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_usage(path: str, data: dict[str, Any]) -> None:
    """原子写：先写临时文件再替换，避免并发/中断留下半截 JSON。"""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:  # 记账失败不能拦住 LLM 调用本身，但必须报出来
        log.warning("LLM 用量记账落盘失败（本次仅进程内计数）：%s", exc)


def _day_key(when: date | None = None) -> str:
    return (when or date.today()).isoformat()


def day_usage(path: str, day: str | None = None) -> dict[str, Any]:
    """读取某日用量（默认今天）。"""
    data = _read_usage(path)
    entry = data.get(day or _day_key()) or {}
    return entry if isinstance(entry, dict) else {}


def check_model_allowed(model: str, cfg: BudgetSettings) -> str:
    """模型门禁：返回拒绝原因，空串表示放行。"""
    name = (model or "").strip().lower()
    if not name:
        return "模型名为空"
    if cfg.allowlist:
        # 有白名单则只认白名单（白名单即「已确认开了用完即停」的清单）
        return "" if name in cfg.allowlist else f"模型 {model} 不在 LLM_MODEL_ALLOWLIST 白名单内"
    # 白名单为空：登记在册的「确认用完即停」免费模型，越过付费关键字子串误判
    # （如 deepseek-v4-pro 含 'pro' 但实为免费档）；具体的类型/到期/开启校验交给 check_model_registry。
    if _registry_has(name) or cfg.allow_paid:
        return ""
    hit = next((h for h in cfg.paid_hints if h and h in name), "")
    return f"模型 {model} 命中付费关键字 '{hit}'（免费额度耗尽会静默转按量计费）" if hit else ""


# ---------------------------------------------------------------------------
# 模型登记册（LLM_MODEL_REGISTRY）：把截图里的「模型|类型|到期|是否开启」变成
# 机器可校验的元数据，落地「判断类型是否可用 / 是否到期 / 是否开启」三道闸。
# 格式：每条 `name|type|expiry|enabled`，多条逗号分隔。
#   type   : llm/text/chat/language/nlp 视为文本对话可用；image/vision/video/audio 等判不可用
#   expiry: YYYY-MM-DD 或 YYYY/MM/DD；留空=不过期
#   enabled: on/1/true/yes = 开启；off/0/false/no/disabled = 关闭（用完即停开关）
# 注意：登记册是**增强型**校验——模型不在登记册里时不强制拦截，只交给上面的
# allowlist/付费关键字逻辑处理，避免误伤未登记的其它 provider（如 deepseek）。
# ---------------------------------------------------------------------------

# 到期时间支持的写法（人工配置友好）：
#   YYYY-MM-DD              → 该日 23:59:59 失效（当天整天可用，与截图「剩余 N 天」口径一致）
#   YYYY-MM-DD HH:MM[:SS]   → 精确到分钟 / 秒
#   YYYY-MM-DDTHH:MM[:SS][.fff][Z] → ISO 风格（Z / 毫秒后缀会被剥掉）
#   YYYY/MM/DD 及带时刻     → 斜杠统一归一化为短横
# 解析不出 → None。**不做「截前 10 字符」的兜底**：那会把 "2026-11-13 25:99" 静默
# 当成 11-13 放行，属于 fail-open（钱相关必须 fail-closed）。
_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S.%f",
)
_DATE_FORMATS = ("%Y-%m-%d",)

# 时区偏移后缀（如 +08:00 / -05:00）：登记册是人工配置的本地时间，偏移只做剥离不做换算
_TZ_OFFSET_SUFFIX = re.compile(r"[+-]\d{2}:?\d{2}$")


def _parse_expire_at(raw: str | None) -> datetime | None:
    """解析到期时间 → datetime。只写日期时按该日 23:59:59 失效；解析不出返回 None。"""
    s = (raw or "").strip().replace("/", "-")
    if not s:
        return None
    if s.endswith(("Z", "z")):  # ISO 的 UTC 标记
        s = s[:-1]
    s = _TZ_OFFSET_SUFFIX.sub("", s).strip()  # 剥掉 +08:00 之类偏移，按本地时间口径
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
    return max(0, _as_int0(os.getenv("LLM_EXPIRY_GUARD_MINUTES"), DEFAULT_EXPIRY_GUARD_MINUTES))


@dataclass
class ExpiryState:
    """单个模型的到期状态（时刻精度）。

    `blocked` 是调用方唯一需要看的结论：**剩余时长小于闸值即为 True**（已过期同样为 True）。
    """

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
        log.warning(
            "LLM_MODEL_REGISTRY 有重复模型名 %s，已按「开启优先/先声明优先」去重",
            sorted(set(duplicated)),
        )
    return reg


def _registry_has(name: str) -> bool:
    """模型是否已登记在 LLM_MODEL_REGISTRY（即「已确认开了用完即停」）。"""
    raw = os.getenv("LLM_MODEL_REGISTRY")
    if not raw:
        return False
    return name in _parse_registry(raw)


# ---------------------------------------------------------------------------
# 免费额度耗尽标记（FreeTierOnly 一等信号）
#
# 为什么需要它：百炼 API **不返回**「本次调用是否免费」字段——调用成功既可能在免费
# 额度内，也可能已在按量计费，二者无法从响应区分。平台唯一给出的权威信号是
# 「免费额度已耗尽且已开启用完即停」时返回的 HTTP 403 + `AllocationQuota.FreeTierOnly`。
#
# 因此把该信号**落盘**成一等状态：命中一次即标记「该模型免费额度已耗尽」，之后每次
# 调用前在本地直接跳过（可继续降级到链上下一个），不必每次撞墙才知道。
# 对外暴露三态：exhausted（已耗尽）/ ok（曾成功调用且未收到耗尽信号）/ unknown（未知）。
# ---------------------------------------------------------------------------


def _quota_state_path() -> Path:
    raw = os.getenv("LLM_QUOTA_STATE_PATH")
    if raw:
        return Path(raw)
    root = Path(__file__).resolve().parent.parent
    return root / "data" / "llm_quota_state.json"


def _as_int0(raw: str | None, default: int) -> int:
    """允许 0 的整数解析（`_as_int` 会把 0 当非法并回退默认值）。"""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _quota_ttl_hours() -> int:
    """标记有效期（小时）。0（默认）= 永久：免费额度按模型固定，耗尽后不会自行恢复。"""
    return _as_int0(os.getenv("LLM_QUOTA_MARKER_TTL_HOURS"), 0)


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

    主判据是错误码 `AllocationQuota.FreeTierOnly`；再用「403 + free quota 文案」兜底，
    防止上游把 code 挪到别的字段。**不把 429 / 普通 quota 误判为耗尽**：那类是可恢复的限流，
    标记成永久耗尽会让好模型被误杀。

    同时兼容两种异常形态：openai SDK（`message` / `body` / `status_code`）与
    httpx（`response.status_code` / `response.text`，body 不在字符串里）。
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
    """落盘标记「该模型免费额度已耗尽」。返回是否标记（模型名为空则 False）。

    落盘失败由 `_write_usage` 内部告警吞掉，不阻断调用——护栏记账不该成为业务失败原因。
    """
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
    log.warning("模型 %s 免费额度已耗尽（%s），已落盘标记，后续调用直接跳过", model, reason)
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
        log.info("已清除 %d 条免费额度耗尽标记（%s）", removed, model or "全部")
    return removed


def _seen_success(model: str, cfg: BudgetSettings) -> bool:
    """该模型是否**成功调用过**（依据用量账本的 by_model，零额外写盘）。"""
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

    注意：`ok` 只代表「有过成功调用且未收到耗尽信号」，**不是平台给的余额证明**——
    百炼不提供余额查询（`/api/v1/quotas` 只有 `usage_limit` 上限，没有剩余量）。
    """
    cfg = cfg or load_settings()
    mark = _quota_mark(model)
    if mark and mark.get("state") == QuotaState.EXHAUSTED.value:
        return QuotaState.EXHAUSTED.value
    return QuotaState.OK.value if _seen_success(model, cfg) else QuotaState.UNKNOWN.value


# 「长期 / 到期留空」的默认有效期（天）。约定：留空按一个月算，一个月后自动失效，
# 避免「截图没写到期 → 被当成永久可用 → 过了期还在调 → 静默转按量计费」。
DEFAULT_EXPIRY_DAYS = 30


def _default_expiry_days() -> int:
    return _as_int(os.getenv("LLM_MODEL_DEFAULT_EXPIRY_DAYS"), DEFAULT_EXPIRY_DAYS)


def _baseline_path() -> Path:
    raw = os.getenv("LLM_REGISTRY_BASELINE_PATH")
    if raw:
        return Path(raw)
    root = Path(__file__).resolve().parent.parent
    return root / "data" / "llm_registry_baseline.json"


def _registry_snapshot_date() -> date | None:
    """截图登记日（`LLM_MODEL_REGISTRY_SNAPSHOT`）：这批模型的「长期」从这天起算。

    语义：**你给截图、我把模型登记进配置的那一天**，不是每天滚动。
    """
    raw = (os.getenv("LLM_MODEL_REGISTRY_SNAPSHOT") or "").strip()
    if not raw:
        return None
    parsed = _parse_expiry(raw)
    if parsed is None:
        log.warning(
            "LLM_MODEL_REGISTRY_SNAPSHOT=%r 无法解析（应写 YYYY-MM-DD），改用按模型首次登记日推算",
            raw,
        )
    return parsed


def _baseline_for(name: str) -> date | None:
    """「长期」模型的基准日：**登记日 + N 天，之后固定不动**；无法确定时返回 None。

    优先级：显式登记日（`LLM_MODEL_REGISTRY_SNAPSHOT`）> 该模型首次被登记到的日期（落盘）。
    关键：绝不能用「每次 today + N 天」——那样永远差 N 天、永不过期。

    **落盘失败必须返回 None（fail-closed）**：若退回 today，下次又会「today + 30 天」，
    等于永不过期，正是这套护栏要防的隐患。`_write_usage` 会吞 OSError，故必须回读校验。
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
                log.warning("登记册基准日损坏（%s=%r），按今天重置", key, raw)
        today = date.today()
        data[key] = today.isoformat()
        _write_usage(path, data)
        if str(_read_usage(path).get(key) or "") != today.isoformat():
            log.error(
                "登记册基准日落盘失败（%s）：无法确定「长期」的起算日，按 fail-closed 拒绝该模型；"
                "请显式配置 LLM_MODEL_REGISTRY_SNAPSHOT 或把到期日写全",
                path,
            )
            return None
    return today


def resolve_expire_at(name: str, raw_expiry: str, *, now: datetime | None = None) -> ExpiryState:
    """解析某模型的实际**截止时刻**，并判定是否应当禁用。

    - 写了时刻：精确判定；
    - 只写日期：按该日 23:59:59 失效（当天整天可用，与截图「剩余 N 天」口径一致）；
    - 写了但解析不出（如「下个月底」「2026-13-45」）：invalid → 调用方 fail-closed 拒绝；
    - 留空：按「登记日 + N 天（默认 30）」的当日 23:59:59 推定 → 一个月后自动失效；
    - **剩余时长小于闸值（默认 60 分钟）即 blocked**：提前禁用，避免一次调用跨过
      到期时刻——跨期部分按量计费不报错，只会悄悄进账单。
    """
    now = now or datetime.now()
    raw = (raw_expiry or "").strip()
    assumed = False
    # 显式声明为 Optional：两个分支都保证非 None（下面各自 return 或赋 datetime），
    # 但 mypy 需要这行才能把「datetime.combine 分支」与「_parse_expire_at 分支」合并成 datetime。
    expire_at: datetime | None
    if not raw:
        base = _baseline_for(name)
        if base is None:  # 基准日无法确定 → fail-closed，绝不退回「今天 + N 天」（那等于永不过期）
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
    """按登记册校验「类型是否文本 / 是否开启 / 是否过期」。返回拒绝原因，空串放行。

    模型若不在登记册中 → 直接放行（交给 allowlist/付费关键字逻辑），不在此强制。
    """
    raw = os.getenv("LLM_MODEL_REGISTRY")
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
    return _type_reject_reason(model, entry["type"]) or _expiry_reject_reason(model, entry)


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
            f"未写到期→按默认 {_default_expiry_days()} 天推定（{_fmt_moment(st.expire_at)} 失效）"
        )
    days_left = (st.expire_at.date() - date.today()).days if st.expire_at else None
    return _remaining_text(minutes_left, days_left)


def _verdict_reasons(model: str, entry: dict[str, Any], st: ExpiryState, qstate: str) -> list[str]:
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

    返回每条含：
      model / type / expiry（日期，向后兼容）/ expire_at（精确时刻）/ days_left /
      minutes_left / enabled / text_capable / expired / expiry_invalid / expiry_assumed /
      near_expiry（临近到期闸触发）/ quota_state / usable_for_text / reason

    排序：先可用的、再不可用的；同组内按**到期时刻升序**（用完即停：先消耗快过期的）。
    """
    cfg = cfg or load_settings()
    reg = _parse_registry(os.getenv("LLM_MODEL_REGISTRY"))
    now = datetime.now()
    today = now.date()
    out: list[dict[str, Any]] = []
    for name, e in reg.items():
        text_ok = e["type"] in TEXT_CAPABLE_TYPES
        enabled = e["enabled"]
        st = resolve_expire_at(name, e["expiry"], now=now)
        expire_at = st.expire_at
        days_left = (expire_at.date() - today).days if expire_at else None
        minutes_left = int(st.seconds_left // 60) if st.seconds_left is not None else None
        qstate = quota_status(name, cfg)
        usable = text_ok and enabled and not st.invalid and not st.blocked and qstate != "exhausted"
        reasons = _verdict_reasons(name, e, st, qstate)
        date_iso = expire_at.date().isoformat() if expire_at else ""
        out.append(
            {
                "model": name,
                "type": e["type"],
                # expiry / expiry_date 保持「日期」语义，向后兼容判定脚本与既有测试
                "expiry": date_iso or (e["expiry"] or "长期"),
                "expiry_date": date_iso,
                "expire_at": expire_at.strftime("%Y-%m-%d %H:%M:%S") if expire_at else "",
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

    - 已登记且到期可解析的模型按 `expire_at` 升序排在前；
    - 未登记 / 到期为空又无推定 / 解析失败的排在后，并**保持原有相对顺序**（稳定）。

    这样即使 `.env` 里 `LLM_MODEL_CHAIN` 是手写顺序，运行期也会按到期先后调用，
    不会出现「先写的新模型被一直用、快到期的反而没用完就作废」。
    """
    if not chain:
        return []
    reg = _parse_registry(os.getenv("LLM_MODEL_REGISTRY"))
    now = datetime.now()
    keyed: list[tuple[bool, datetime, int, str]] = []
    for idx, raw in enumerate(chain):
        name = (raw or "").strip()
        if not name:
            continue
        entry = reg.get(name.lower())
        expire_at = (
            resolve_expire_at(name.lower(), entry["expiry"], now=now).expire_at if entry else None
        )
        keyed.append((expire_at is None, expire_at or datetime.max, idx, name))
    keyed.sort(key=lambda x: (x[0], x[1], x[2]))
    return [x[3] for x in keyed]


def resolve_text_chain(cfg: BudgetSettings | None = None) -> list[str]:
    """当未显式配置 LLM_MODEL_CHAIN 时，从登记册自动推导文本链路：
    文本可用 + 开启 + 未过期，按到期升序（用完即停：先消耗快过期的）。
    """
    cfg = cfg or load_settings()
    return [v["model"] for v in evaluate_registry(cfg) if v["usable_for_text"]]


def quota_reject_reason(model: str) -> str:
    """免费额度耗尽标记 → 拒绝原因；无标记返回空串。

    抽成公开函数是为了让**不经过 check_call** 的调用方（如 judge 引擎）
    复用同一条判定，避免出现「主链路拦、旁路照跑」的漏口。
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
    """调用前准入检查。不允许时返回 allowed=False，由调用方抛 BudgetExceeded。"""
    cfg = cfg or load_settings()
    est = estimate_messages_tokens(messages)

    reason = check_model_allowed(model, cfg)
    if reason:
        return BudgetDecision(False, reason, model, est, scope="model")

    # 登记册增强校验：类型是否文本 / 是否开启 / 是否过期 / 是否临近到期
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
    """记一次成功调用。prompt/completion 来自 response.usage；缺失时传估算值。"""
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
        # 只保留最近 N 天
        cutoff = (date.today() - timedelta(days=_RETAIN_DAYS)).isoformat()
        data = {k: v for k, v in data.items() if k >= cutoff}
        _write_usage(cfg.usage_path, data)
    if total:
        log.info(
            "LLM 用量 +%d token（模型 %s，通道 %s）；当日累计 %d/%d",
            total,
            model,
            channel or "-",
            int(entry.get("total") or 0),
            cfg.daily_token_budget,
        )
    return entry


def snapshot(cfg: BudgetSettings | None = None) -> dict[str, Any]:
    """当日用量快照（供健康检查/调试展示）。"""
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
