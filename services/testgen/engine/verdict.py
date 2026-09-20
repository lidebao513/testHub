"""五维安全判定薄封装（M2：/api/v1/verdict 后端）。

铁律：判定**逻辑完全复用** ``engine.executor`` 的既有纯函数（``_judge`` / ``_dimension_kind`` /
``_PASS_PREDICATES`` / ``_VERDICT_DESC``）。本模块只做「testhub 观测事实 → 安全语义裁判」的适配、
双语归一化与格式化，**不重写任何判定规则**——保证与 testgen 既有执行判定 100% 一致。

testgen 原生枚举是中文（``TPType.SECURITY.value="安全"``、``Dimension.PRIV_ESC.value="安全-越权"``），
但调用方（testhub / curl）可能传英文别名。``_normalize_*`` 在调用 ``_judge`` 前把入参归一到
testgen 原生中文词汇，避免「英文 category 落不到 _CATEGORY_KIND 而被误判为 normal」这类漂移。
"""
from __future__ import annotations

from typing import Any

# 复用 executor 既有判定纯函数（不复制、不重写）
from engine.executor import _dimension_kind, _judge

# ---------------------------------------------------------------------------
# 安全语义裁判取值
# ---------------------------------------------------------------------------
VERDICT_SAFE = "safe"
VERDICT_UNSAFE = "unsafe"
VERDICT_INCONCLUSIVE = "inconclusive"

# 需要「被拒绝」才安全的维度（无凭证访问 / 越权）
_DENY_KINDS = frozenset({"auth", "priv_esc"})

# ---------------------------------------------------------------------------
# 双语归一化：调用方可能传中文（testgen 原生）或英文别名，统一回中文词汇
# ---------------------------------------------------------------------------
_CATEGORY_NORMALIZE: dict[str, str] = {
    "正常": "正常", "normal": "正常",
    "异常": "异常", "abnormal": "异常",
    "安全": "安全", "security": "安全",
    "边界": "边界", "boundary": "边界",
    "性能": "性能", "performance": "性能",
}

# 子维度：英文别名 → testgen 原生中文（供 _dimension_kind 的 PRIV_ESC 子串识别）
_DIM_NORMALIZE: dict[str, str] = {
    "priv_esc": "安全-越权", "越权": "安全-越权", "privilege_escalation": "安全-越权",
    "auth_miss": "安全-鉴权缺失", "鉴权缺失": "安全-鉴权缺失", "auth_missing": "安全-鉴权缺失",
    "token_expired": "安全-令牌过期", "令牌过期": "安全-令牌过期",
    "param_illegal": "边界-参数缺失/非法", "参数缺失/非法": "边界-参数缺失/非法",
    "ui_unauth_page": "安全-未授权访问", "未授权访问": "安全-未授权访问",
    "ui_input_inject": "安全-输入注入", "输入注入": "安全-输入注入",
}


def _normalize_category(category: str) -> str:
    return _CATEGORY_NORMALIZE.get((category or "").strip().lower(), (category or "").strip())


def _normalize_dimension(dimension: str) -> str:
    key = (dimension or "").strip().lower()
    return _DIM_NORMALIZE.get(key, (dimension or "").strip())


def run_five_dimension_verdict(facts: dict[str, Any]) -> dict[str, Any]:
    """把 testhub 执行 security 用例后回传的「观测事实」转成安全语义裁判。

    ``facts`` 字段（均为 testhub 侧观测，**不含任何凭证明文**）：
    - ``category``：行为维度大类（正常/异常/安全/边界/性能，亦接受英文别名）
    - ``dimension``：子维度（如 安全-越权 / priv_esc / 安全-鉴权缺失）
    - ``observed_status``：执行后实际 HTTP 状态码（无则无法裁判）
    - ``auth_mode``：期望鉴权接线（required/optional/absent）
    - ``expected_denied``：该用例是否期望被拒绝（无凭证/越权）
    - ``observed_body_has_sensitive``：响应体是否疑似泄露敏感信息
    - ``tenant_identity``：越权复测双身份标识（仅作证据，不持有执行会话）
    - ``raw_evidence``：调用方自由附带的额外证据
    """
    category = _normalize_category(str(facts.get("category") or ""))
    dimension = _normalize_dimension(str(facts.get("dimension") or ""))
    auth_mode = str(facts.get("auth_mode") or "")
    expected_denied = bool(facts.get("expected_denied", False))
    body_sensitive = bool(facts.get("observed_body_has_sensitive", False))
    tenant_identity = str(facts.get("tenant_identity") or "")
    raw_evidence = facts.get("raw_evidence") or {}
    status = facts.get("observed_status")

    # 无观测状态：用例未真正执行（被跳过/未跑），诚实返回不可判定
    if status is None:
        kind = _dimension_kind(category, dimension, auth_mode) if category else "unknown"
        return {
            "verdict": VERDICT_INCONCLUSIVE,
            "dimension": kind,
            "reason": "缺少 observed_status，无法做安全判定（用例可能未执行/被跳过）",
            "evidence": {
                "kind": kind,
                "expected_denied": expected_denied,
                "auth_mode": auth_mode,
                "tenant_identity": tenant_identity,
                "raw": raw_evidence,
            },
        }

    status = int(status)
    kind = _dimension_kind(category, dimension, auth_mode)
    passed, detail = _judge(category, dimension, status, auth_mode)

    verdict = VERDICT_SAFE if passed else VERDICT_UNSAFE
    if expected_denied:
        if not passed:
            # 期望被拒却放通 = 安全漏洞（应拒却放通）
            reason = f"应被拒绝却放通：{detail}"
        else:
            reason = f"已按预期拒绝：{detail}"
    else:
        reason = detail

    evidence: dict[str, Any] = {
        "kind": kind,
        "observed_status": status,
        "expected_denied": expected_denied,
        "auth_mode": auth_mode,
        "tenant_identity": tenant_identity,
        "raw": raw_evidence,
    }

    # 响应体疑似泄露敏感信息：无论状态码如何，直接判不安全
    if body_sensitive:
        verdict = VERDICT_UNSAFE
        evidence["leak_suspected"] = True
        reason = (reason + "；响应体疑似含敏感信息").strip("；")

    return {
        "verdict": verdict,
        "dimension": kind,
        "reason": reason,
        "evidence": evidence,
    }
