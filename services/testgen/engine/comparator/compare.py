"""LLM 语义比对器（comparator）。

纯函数模块：输入「预期(case) + 实际执行结果(actual)」，输出「语义比对结论」。
不碰 DB / pipeline / 执行动作；依赖仅 `engine.llm_fallback.chat_with_fallback`（统一降级链）。

设计要点（见 docs/comparator_modularization_design.md）：
- 规则为主 · LLM 增强：executor 的规则判定始终保留在 `rule_verdict`。
- 诚实降级：LLM 超时/被墙/非 JSON/解析失败 → 回退规则判定并标 `inconclusive`，**绝不假装通过**。
- 安全红线：I/O 中 password/otp/api_key/token 等一律脱敏（参考 executor）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from core.errors import LLMError
from core.log import get_logger
from engine.llm_fallback import chat_with_fallback


log = get_logger(__name__)

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_PARTIAL = "partial"
VERDICT_INCONCLUSIVE = "inconclusive"
_VALID = frozenset({VERDICT_PASS, VERDICT_FAIL, VERDICT_PARTIAL, VERDICT_INCONCLUSIVE})

# 敏感字段名（脱敏用，仅作正则关键字，非凭据）
_SENSITIVE_KEY = re.compile(
    r'("(?:password|passwd|pwd|otp|token|api[_-]?key|secret|authorization|cookie|session|access[_-]?token)'
    r'\s*"\s*:\s*)(?:"[^"]*"|[^\s,}\]]+)',
    re.IGNORECASE,
)


def redact(text: str) -> str:
    """脱敏：保留敏感字段名，值替换为 ***。"""
    if not text:
        return text
    return _SENSITIVE_KEY.sub(r'\1"***"', text)


@dataclass
class CompareInput:
    """比对输入。

    case: 预期侧，来自 CaseSpec —— id/title/steps[].expect/steps[].dimension/severity。
    actual: 实际侧，来自 executor.ExecutionResult.to_dict() —— status/notes/step_results/error。
    context: 可选覆盖（model、language、project_id 等）。
    """

    case: dict
    actual: dict
    context: dict = field(default_factory=dict)


@dataclass
class CompareVerdict:
    verdict: str
    confidence: float
    reason: str
    diff: list[str] = field(default_factory=list)
    rule_verdict: str = ""
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "confidence": self.confidence,
            "reason": self.reason,
            "diff": list(self.diff),
            "rule_verdict": self.rule_verdict,
            "model": self.model,
        }


@dataclass
class ComparatorOptions:
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    model_chain: list[str] = field(default_factory=list)
    timeout: float = 120.0
    enabled: bool = True
    temperature: float = 0.2

    @classmethod
    def from_settings(cls, settings: Any | None = None) -> ComparatorOptions:
        """从 Settings 读取专家通道配置（comparator 复用专家 LLM 通道）。"""
        if settings is None:
            try:
                from core.config import get_settings

                settings = get_settings()
            except Exception:  # noqa: BLE001 - 无配置时退化为未启用
                return cls(enabled=False)
        chain = list(getattr(settings, "expert_model_chain", None) or [])
        model = getattr(settings, "expert_model", None) or (chain[0] if chain else "")
        return cls(
            api_key=getattr(settings, "expert_api_key", "") or "",
            base_url=getattr(settings, "expert_base_url", "") or "",
            model=model,
            model_chain=chain,
            timeout=float(getattr(settings, "expert_timeout", 120.0) or 120.0),
        )


def _rule_verdict_from_actual(actual: dict) -> str:
    """executor 的 status 归一化为比对器判定词。"""
    status = str((actual or {}).get("status", "")).upper()
    if status == "PASS":
        return VERDICT_PASS
    if status == "FAIL":
        return VERDICT_FAIL
    return VERDICT_INCONCLUSIVE  # SKIPPED / ERROR / 未知


def _summarize_actual(actual: dict) -> str:
    parts: list[str] = []
    if "status" in actual:
        parts.append(f"执行状态: {actual['status']}")
    notes = actual.get("notes")
    if isinstance(notes, list):
        parts.extend([f"- {n}" for n in notes[:8]])
    elif notes:
        parts.append(str(notes))
    for key in ("error", "response_body", "dom_text", "console"):
        val = actual.get(key)
        if val:
            parts.append(f"{key}: {str(val)[:800]}")
    return "\n".join(parts)


def _build_messages(inp: CompareInput) -> list[dict[str, str]]:
    case = inp.case or {}
    steps = case.get("steps") or []
    expect = case.get("expect") or (steps[0].get("expect") if steps else "")
    dimension = case.get("dimension") or (steps[0].get("dimension") if steps else "")
    title = case.get("title", "")
    case_id = case.get("id", "")
    actual_summary = _summarize_actual(inp.actual or {})
    system = (
        "你是资深测试分析专家。请对比「预期结果」与「实际执行结果」，给出结构化判定。"
        "只输出 JSON，不要额外说明。JSON 结构："
        '{"verdict":"pass|fail|partial|inconclusive",'
        '"confidence":0.0~1.0,'
        '"reason":"中文一句话解释",'
        '"diff":["关键差异点1","关键差异点2"]}'
        "规则：实际与预期一致→pass；明显不符→fail；部分符合或需人工确认→partial；"
        "信息不足以判定→inconclusive。诚实优先，绝不臆测通过。"
    )
    user = (
        f"用例ID: {case_id}\n标题: {title}\n维度: {dimension}\n"
        f"【预期结果】\n{expect}\n\n"
        f"【实际执行结果】\n{actual_summary}\n\n"
        "请输出判定 JSON。"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _extract_json(content: str) -> dict:
    if not content:
        raise ValueError("empty llm content")
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no json object found in llm response")
    return json.loads(text[start : end + 1])


def _normalize_llm_verdict(data: dict) -> tuple[str, float, str, list[str]]:
    raw = str(data.get("verdict", "")).lower()
    if raw not in _VALID:
        raise ValueError(f"invalid verdict: {raw!r}")
    try:
        conf = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    reason = str(data.get("reason", ""))[:2000]
    diff = [str(x) for x in (data.get("diff") or [])][:20]
    return raw, conf, reason, diff


def compare_one(inp: CompareInput, opts: ComparatorOptions | None = None) -> CompareVerdict:
    opts = opts or ComparatorOptions.from_settings()
    rule = _rule_verdict_from_actual(inp.actual or {})

    # 未启用 / 未配置 → 仅规则判定，不触 LLM（保持确定性、零外部依赖）
    if not opts.enabled or not opts.api_key or not opts.base_url:
        return CompareVerdict(
            verdict=rule,
            confidence=1.0 if rule in (VERDICT_PASS, VERDICT_FAIL) else 0.0,
            reason="规则判定（comparator 未启用或未配置 LLM）",
            diff=[],
            rule_verdict=rule,
            model="",
        )

    messages = _build_messages(inp)
    try:
        resp = chat_with_fallback(
            channel="comparator",
            api_key=opts.api_key,
            base_url=opts.base_url,
            timeout=opts.timeout,
            model=opts.model,
            model_chain=opts.model_chain,
            messages=messages,
            temperature=opts.temperature,
        )
        content = resp.choices[0].message.content
        data = _extract_json(content)
        verdict, conf, reason, diff = _normalize_llm_verdict(data)
        model = getattr(resp, "model", None) or opts.model
        return CompareVerdict(
            verdict=verdict,
            confidence=conf,
            reason=redact(reason),
            diff=[redact(d) for d in diff],
            rule_verdict=rule,
            model=model,
        )
    except LLMError as exc:
        # 诚实降级：额度/超时/真实错误 → 以规则判定为准，绝不假装通过
        return CompareVerdict(
            verdict=VERDICT_INCONCLUSIVE,
            confidence=0.0,
            reason=f"LLM 语义比对降级（{type(exc).__name__}）：以规则判定 {rule} 为准",
            diff=[],
            rule_verdict=rule,
            model="",
        )
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        return CompareVerdict(
            verdict=VERDICT_INCONCLUSIVE,
            confidence=0.0,
            reason=f"LLM 返回无法解析（{type(exc).__name__}）：以规则判定 {rule} 为准",
            diff=[],
            rule_verdict=rule,
            model="",
        )


def compare_batch(
    inputs: list[CompareInput], opts: ComparatorOptions | None = None
) -> list[CompareVerdict]:
    return [compare_one(i, opts) for i in inputs]
