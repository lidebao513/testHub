"""testgen-capabilities 包内自测。

验证：能力层可在「无存储、无网络」前提下工作（纯函数 + 规则判定路径）。
不依赖 testgen 的 projects/cases/runs 表，满足 P2-1 红线。
"""

from testgen_capabilities import (
    ComparatorOptions,
    CompareInput,
    DialogueAgent,
    compare_one,
    default_dialogue_template,
    redact,
)


def test_comparator_rule_only_pass_no_network():
    inp = CompareInput(case={"id": "c1", "title": "t"}, actual={"status": "PASS"})
    v = compare_one(inp, ComparatorOptions(enabled=False))
    assert v.verdict == "pass"
    assert v.rule_verdict == "pass"
    assert v.model == ""  # 未触 LLM


def test_comparator_rule_only_fail_no_network():
    inp = CompareInput(case={}, actual={"status": "FAIL"})
    v = compare_one(inp, ComparatorOptions(enabled=False))
    assert v.verdict == "fail"


def test_redact_masks_secrets():
    out = redact('{"password":"hunter2","token":"abc","note":"ok"}')
    assert "hunter2" not in out
    assert "abc" not in out
    assert '"password"' in out
    assert '"***"' in out


def test_dialogue_template_render_then_validate_url():
    tpl = default_dialogue_template()
    rendered = tpl.render_template_text()
    assert "source_kind" in rendered
    assert "test_url" in rendered

    plan = tpl.validate_and_parse(
        {
            "source_kind": "url",
            "mode": "full",
            "test_url": "http://example.com",
            "login_user": "u",
            "login_password": "p",
        }
    )
    assert plan.ok, plan.errors
    assert plan.params["test_url"] == "http://example.com"
    assert plan.redacted["login_password"] == "***"  # 凭证脱敏视图


def test_dialogue_agent_text_fallback_masks_credentials():
    agent = DialogueAgent()
    res = agent.handle_text("被测地址 http://21.163.93.12:8099 账号 admin 密码 secret")
    assert res.ok, res.errors
    assert res.params.get("test_url") == "http://21.163.93.12:8099"
    # 凭证绝不进入明文产物
    assert res.redacted.get("login_password") == "***"
