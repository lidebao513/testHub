"""PW-2：断言来源纪律护栏（纯函数，不依赖 Django / browser-use）。

背景：#181 三层断言是「AI 自称完成」之外的独立证据。若修复/自愈流程为了
让用例通过而删除或放宽断言，就会产生假阳性。本模块提供：

- ``is_critical_business``：关键业务识别（支付/订单/结算），命中即禁止改断言；
- ``snapshot_assertions``：按 ``(level, type, value, task_id)`` 生成稳定指纹；
- ``guard_assertion_change``：关键业务禁止改断言；非关键放行但记录来源与 diff。

设计约束：
- **纯函数、无副作用**：不写库、不改入参、不抛异常（fail-open 由调用点负责）；
- **不误伤**：status/detail 是运行时评估结果，不参与指纹——同一断言两次执行
  结果不同不算「改断言」；只有断言本身（level/type/value/task_id）变化才算；
- **只增字段**：不破坏旧报告结构，消费者可忽略新键。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 关键业务关键词表（大小写不敏感；新业务可追加）。
CRITICAL_KEYWORDS = (
    "支付",
    "付款",
    "订单",
    "结算",
    "金额",
    "交易",
    "pay",
    "order",
    "checkout",
    "payment",
)

# 参与指纹的断言字段：只含「断言本身」，不含 status/detail 等评估结果。
_FINGERPRINT_FIELDS = ("level", "type", "value", "task_id")


class AssertionMutationError(Exception):
    """断言被违规修改时由严格模式抛出；软护栏路径只记录不抛。"""


def is_critical_business(text: str) -> bool:
    """判断任务描述/用例名是否属于关键业务。大小写不敏感。"""
    if not text:
        return False
    lowered = str(text).lower()
    return any(keyword in lowered for keyword in CRITICAL_KEYWORDS)


def _item_identity(item: dict) -> tuple:
    """取一条断言的稳定标识；脏数据用空值兜底，保证可比较。"""
    if not isinstance(item, dict):
        return tuple("" for _ in _FINGERPRINT_FIELDS)
    return tuple(str(item.get(key) or "") for key in _FINGERPRINT_FIELDS)


def normalize_items(report: dict | None) -> list[dict]:
    """从报告 dict 里取出断言条目（只保留 dict，脏数据丢弃）。"""
    if not isinstance(report, dict):
        return []
    items = report.get("items") or []
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict)]


def snapshot_assertions(report: dict | None) -> str:
    """报告 → 稳定指纹（sha256[:16]）；顺序无关，仅断言本身参与。"""
    identities = sorted(_item_identity(item) for item in normalize_items(report))
    payload = "|".join(",".join(ident) for ident in identities)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class AssertionGuardResult:
    """护栏判定结果。

    allowed      是否放行新断言（critical 违规时为 False）
    blocked      是否被拦截（与 allowed 相反，便于调用方直接读）
    changes      断言变化明细（before/after/source/reason）
    final_report 调用方应落库的报告（blocked 时保留基准 items）
    reason       拦截/放行原因说明
    """

    allowed: bool = True
    blocked: bool = False
    changes: list[dict] = field(default_factory=list)
    final_report: dict = field(default_factory=dict)
    reason: str = ""


def diff_assertion_items(before: dict | None, after: dict | None) -> list[dict]:
    """逐条对比两组断言，返回变化的明细（顺序无关）。

    每条变化含 ``{field, before, after, source, reason}``：
    - field: 断言类型（如 page_contains），便于阅读；
    - before/after: 变化的断言 dict（新增时 before=None，删除时 after=None）；
    - source: 断言来源（优先取 after，其次 before）；
    - reason: added / removed / modified。
    """
    before_items = normalize_items(before)
    after_items = normalize_items(after)
    before_map = {_item_identity(i): i for i in before_items}
    after_map = {_item_identity(i): i for i in after_items}

    changes: list[dict] = []
    for ident in sorted(set(before_map) | set(after_map)):
        old = before_map.get(ident)
        new = after_map.get(ident)
        if old is not None and new is not None:
            # 身份相同 → 断言本身没变（status/detail 变化不算），不记 diff。
            continue
        source = str(
            (new or {}).get("source")
            or (old or {}).get("source")
            or (new or {}).get("level")
            or (old or {}).get("level")
            or "unknown"
        )
        changes.append(
            {
                "field": ident[1] or "assertion",
                "before": old,
                "after": new,
                "source": source,
                "reason": "added" if old is None else "removed",
            }
        )
    return changes


def guard_assertion_change(
    before: dict | None,
    after: dict | None,
    *,
    critical: bool,
    strict: bool = False,
) -> AssertionGuardResult:
    """比对基准断言与新断言并给出护栏判定。

    - ``critical=True`` 且断言指纹变化 → 拦截：``final_report`` 保留基准 items，
      并在报告里记 ``blocked_assertion_change``；``strict=True`` 时抛
      ``AssertionMutationError``；
    - ``critical=False`` 且变化 → 放行，但把 diff 追加到报告的 ``assertion_changes``；
    - 无变化 / 无基准 → 直通，不产生 changes。
    """
    new_report = dict(after or {})
    if before is None or after is None:
        return AssertionGuardResult(final_report=new_report, reason="no_baseline")

    if snapshot_assertions(before) == snapshot_assertions(after):
        return AssertionGuardResult(final_report=new_report, reason="unchanged")

    changes = diff_assertion_items(before, after)
    source = changes[0]["source"] if changes else "unknown"

    if critical:
        if strict:
            raise AssertionMutationError(
                f"关键业务禁止修改断言（来源={source}，变化={len(changes)}条）"
            )
        merged = dict(new_report)
        merged["items"] = normalize_items(before)
        merged["blocked_assertion_change"] = {
            "reason": "critical_business_assertion_locked",
            "source": source,
            "count": len(changes),
            "before_fp": snapshot_assertions(before),
            "after_fp": snapshot_assertions(after),
        }
        logger.warning("关键业务断言变更被拦截：来源=%s 变化=%d条", source, len(changes))
        return AssertionGuardResult(
            allowed=False,
            blocked=True,
            changes=changes,
            final_report=merged,
            reason="critical_business_assertion_locked",
        )

    existing = new_report.get("assertion_changes") or []
    if not isinstance(existing, list):
        existing = []
    merged = dict(new_report)
    merged["assertion_changes"] = [*existing, *changes]
    logger.info("非关键业务断言变更已记录：%d 条", len(changes))
    return AssertionGuardResult(
        changes=changes,
        final_report=merged,
        reason="non_critical_change_recorded",
    )
