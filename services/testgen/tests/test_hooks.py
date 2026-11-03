"""G-4 执行后安全裁判钩子单元测试（离线，不依赖 LLM / 网络 / DB）。

覆盖：
1. ``scan_text`` 各类规则命中（明文凭据 / 私钥块 / 危险指令）与未命中（无假阳性）；
2. ``post_execution_security_judge`` 聚合：高危命中 → flagged，全 benign → passed；
3. fail-closed：扫描自身异常不静默通过，记 inconclusive + error；
4. ``cases`` 入参经 ``_case_text`` 序列化后参与扫描。
"""

from __future__ import annotations

from types import SimpleNamespace

from engine import hooks


# ---------------------------------------------------------------------------
# scan_text 规则命中 / 未命中
# ---------------------------------------------------------------------------
def test_scan_text_aws_access_key():
    hits = hooks.scan_text("cfg = AKIAIOSFODNN7EXAMPLEKEY")
    rules = {f.rule for f in hits}
    assert "aws_access_key" in rules
    assert any(f.severity == hooks.SEVERITY_HIGH for f in hits)


def test_scan_text_private_key_block():
    hits = hooks.scan_text("key = -----BEGIN RSA PRIVATE KEY-----\nabc")  # nosec
    assert "private_key_block" in {f.rule for f in hits}


def test_scan_text_dsn_with_credential():
    hits = hooks.scan_text("db = mysql://root:s3cret@127.0.0.1:3306/app")
    assert "dsn_with_credential" in {f.rule for f in hits}


def test_scan_text_dangerous_drop_table():
    hits = hooks.scan_text("清理脚本： DROP TABLE sessions;")
    assert "drop_table" in {f.rule for f in hits}
    assert any(f.severity == hooks.SEVERITY_HIGH for f in hits)


def test_scan_text_assigned_secret_is_medium_only():
    # 赋值式 secret 是 medium，不应触发 high → 整体 verdict 仍 passed
    hits = hooks.scan_text("header x-api-key = abcdefghijklmnop")
    rules = {f.rule for f in hits}
    assert "assigned_secret" in rules
    assert all(f.severity == hooks.SEVERITY_MEDIUM for f in hits)


def test_scan_text_benign_no_false_positive():
    text = "用例：GET /api/v1/users 期望 200；用户名长度 3-20 位"
    assert hooks.scan_text(text) == []


# ---------------------------------------------------------------------------
# post_execution_security_judge 聚合
# ---------------------------------------------------------------------------
def test_judge_flagged_on_high_severity():
    rep = hooks.post_execution_security_judge(texts=["leak AKIAIOSFODNN7EXAMPLEKEY here"])
    assert rep.verdict == "flagged"
    assert rep.checked == 1
    assert any(f.rule == "aws_access_key" for f in rep.findings)


def test_judge_passed_on_benign():
    rep = hooks.post_execution_security_judge(
        texts=["GET /api/v1/users 期望 200", "用户名长度 3-20 位"]
    )
    assert rep.verdict == "passed"
    assert rep.checked == 2
    assert rep.findings == []


def test_judge_skips_empty_texts():
    rep = hooks.post_execution_security_judge(texts=["", "   "])
    assert rep.verdict == "passed"
    assert rep.checked == 0


def test_judge_fail_closed_on_scan_exception():
    # fail-closed：扫描函数自身抛异常 → 记 inconclusive，不静默通过
    def _boom(_text: str) -> list:
        raise RuntimeError("scanner exploded")

    orig = hooks.scan_text
    hooks.scan_text = _boom
    try:
        rep = hooks.post_execution_security_judge(texts=["anything"])
    finally:
        hooks.scan_text = orig
    assert rep.verdict == "inconclusive"
    assert rep.error is not None and "scanner exploded" in rep.error


# ---------------------------------------------------------------------------
# cases 入参经 _case_text 参与扫描
# ---------------------------------------------------------------------------
def _fake_case(title: str, steps: list) -> SimpleNamespace:
    return SimpleNamespace(title=title, case_type="api", ctype="api", steps=steps)


def test_judge_with_cases_scans_case_text():
    case = _fake_case(
        title="导出全部",
        steps=[{"method": "GET", "path": "/api/v1/data", "note": "token=supersecretvalue123"}],
    )
    rep = hooks.post_execution_security_judge(cases=[case])
    assert rep.checked == 1
    # assigned_secret 为 medium，非高危 → passed（验证 case 文本确实被纳入）
    assert rep.verdict == "passed"
    assert "assigned_secret" in {f.rule for f in rep.findings}


def test_judge_with_cases_high_severity_flagged():
    case = _fake_case(
        title="危险清理",
        steps=[{"method": "POST", "path": "/api/v1/reset", "note": "rm -rf /var/data"}],
    )
    rep = hooks.post_execution_security_judge(cases=[case])
    assert rep.verdict == "flagged"
    assert "rm_rf" in {f.rule for f in rep.findings}
