"""G-4 执行后安全裁判钩子（post-execution security judge）。

在两条引擎（生成 ``run_pipeline`` / 执行 ``run_execution``）成功路径收尾处调用，对产出的
测试资产做「安全裁判」：扫描生成 / 执行产物是否夹带明文凭据、危险指令或对越权证据的泄露。

铁律（与项目 fail-closed 偏好一致）：
- 裁判自身异常 → 不静默通过，记 ``verdict=inconclusive`` 并标记 ``error``，交由上游人工复核；
- 命中高危规则（明文凭据 / 危险指令）→ ``verdict=flagged``（阻断需人工确认）；
- 其余 → ``verdict=passed``，附 ``findings`` 供审计。

本模块不重写任何既有判定规则（与 G-3 ``verdict.py`` 复用 ``executor`` 纯函数的原则一致），
只新增「生成 / 执行产物安全扫描」这一裁判维度；钩子调用方负责把报告挂到产出上。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# 检测规则（与 ``scripts/check_secrets.py`` 同口径的精简子集；只覆盖「生成物会夹带」的高危类）
# 每条： (规则名, 严重度, 编译后的正则)
# ---------------------------------------------------------------------------
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"

_SECRET_RULES: list[tuple[str, str, re.Pattern[str]]] = [
    ("aws_access_key", SEVERITY_HIGH, re.compile(r"AKIA[0-9A-Z]{16}")),
    (
        "private_key_block",
        SEVERITY_HIGH,
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    ("bearer_token", SEVERITY_HIGH, re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-_.]{16,}")),
    (
        "dsn_with_credential",
        SEVERITY_HIGH,
        re.compile(r"(?i)(?:mysql|postgres|redis|mongodb)://[^\s:@/]+:[^\s:@/]+@"),
    ),
    (
        "assigned_secret",
        SEVERITY_MEDIUM,
        re.compile(
            r"(?i)(?:api[_-]?key|secret|token|password|passwd)\s*[=:]\s*['\"]?[A-Za-z0-9\-_.]{12,}"
        ),
    ),
]

_DANGEROUS_RULES: list[tuple[str, str, re.Pattern[str]]] = [
    ("drop_table", SEVERITY_HIGH, re.compile(r"(?i)\bdrop\s+table\b")),
    ("truncate_table", SEVERITY_HIGH, re.compile(r"(?i)\btruncate\s+table\b")),
    ("delete_without_where", SEVERITY_HIGH, re.compile(r"(?i)delete\s+from\s+\w+\s*;?\s*$")),
    ("rm_rf", SEVERITY_HIGH, re.compile(r"(?i)\brm\s+-rf\b")),
    ("git_force_push", SEVERITY_MEDIUM, re.compile(r"(?i)git\s+push\s+--force\b")),
]


@dataclass
class Finding:
    """单条命中。"""

    rule: str
    severity: str
    snippet: str

    def to_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "severity": self.severity, "snippet": self.snippet}


@dataclass
class SecurityJudgeReport:
    """安全裁判报告。"""

    verdict: str  # passed | flagged | inconclusive
    checked: int = 0
    findings: list[Finding] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "checked": self.checked,
            "findings": [f.to_dict() for f in self.findings],
            "error": self.error,
        }


def scan_text(text: str) -> list[Finding]:
    """对单段文本跑全部规则，返回命中（每条规则至多 1 条，避免噪声爆炸）。

    命中片段前后各保留 8 个字符作为上下文，便于人工复核时定位。
    """
    findings: list[Finding] = []
    for name, sev, pat in _SECRET_RULES + _DANGEROUS_RULES:
        m = pat.search(text)
        if m:
            start = max(0, m.start() - 8)
            end = min(len(text), m.end() + 8)
            findings.append(
                Finding(rule=name, severity=sev, snippet=text[start:end].replace("\n", " "))
            )
    return findings


def _case_text(case: Any) -> str:
    """把一条 CaseSpec 的关键字段拼成待扫描文本（防御式，缺字段不报错）。"""
    parts: list[str] = []
    for attr in ("title", "case_type", "ctype", "precondition", "expect"):
        v = getattr(case, attr, None)
        if v:
            parts.append(str(v))
    steps = getattr(case, "steps", None)
    if steps:
        parts.append(str(steps))
    return "\n".join(parts)


def post_execution_security_judge(
    *,
    cases: list[Any] | None = None,
    texts: list[str] | None = None,
) -> SecurityJudgeReport:
    """执行后安全裁判：扫描生成 / 执行的测试资产。

    Args:
        cases: 生成的测试用例（任意带 ``title`` / ``steps`` 等属性的对象，如 ``CaseSpec``）。
        texts: 额外的待扫描文本（如落库摘要、执行证据片段）。

    Returns:
        SecurityJudgeReport。fail-closed：扫描自身抛异常不静默通过，记
        ``inconclusive`` + ``error``，交由上游人工复核。
    """
    try:
        collected: list[str] = list(texts or [])
        for case in cases or []:
            collected.append(_case_text(case))
        findings: list[Finding] = []
        checked = 0
        for text in collected:
            if not text or not text.strip():
                continue
            checked += 1
            findings.extend(scan_text(text))
        # 任一高危命中即标记为需人工确认（flagged）；否则视为通过（passed）
        verdict = "flagged" if any(f.severity == SEVERITY_HIGH for f in findings) else "passed"
        return SecurityJudgeReport(verdict=verdict, checked=checked, findings=findings)
    except Exception as exc:  # fail-closed：任何异常都标记不可判定，不静默通过
        return SecurityJudgeReport(verdict="inconclusive", error=str(exc))
