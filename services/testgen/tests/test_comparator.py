from __future__ import annotations

import types
from unittest.mock import patch

from core.errors import LLMError
from engine.comparator import (
    ComparatorOptions,
    CompareInput,
    compare_batch,
    compare_one,
    redact,
)


def _resp(text: str, model: str = "qwen3.8-flash"):
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text))],
        model=model,
    )


_GOOD = '{"verdict":"pass","confidence":0.92,"reason":"页面正常渲染，符合预期","diff":["无差异"]}'
_FAIL = '{"verdict":"fail","confidence":0.88,"reason":"返回404，预期200","diff":["状态码404≠200"]}'


def _case() -> dict:
    return {
        "id": "C1",
        "title": "正常渲染",
        "steps": [{"expect": "页面返回200并渲染", "dimension": "normal"}],
    }


def _actual_pass() -> dict:
    return {"status": "PASS", "notes": ["GET /x -> 200"]}


def _actual_fail() -> dict:
    return {"status": "FAIL", "notes": ["GET /x -> 404"]}


def _opts() -> ComparatorOptions:
    return ComparatorOptions(
        api_key="k", base_url="https://x", model="m0", model_chain=["m0", "m1"]
    )


def test_llm_pass_preserves_rule_verdict():
    with patch("engine.comparator.compare.chat_with_fallback", return_value=_resp(_GOOD)):
        v = compare_one(CompareInput(case=_case(), actual=_actual_pass()), _opts())
    assert v.verdict == "pass"
    assert v.rule_verdict == "pass"
    assert v.model == "qwen3.8-flash"
    assert v.confidence == 0.92


def test_llm_fail_verdict():
    with patch("engine.comparator.compare.chat_with_fallback", return_value=_resp(_FAIL)):
        v = compare_one(CompareInput(case=_case(), actual=_actual_fail()), _opts())
    assert v.verdict == "fail"
    assert v.rule_verdict == "fail"


def test_llm_error_degrades_to_inconclusive_never_pass():
    with patch("engine.comparator.compare.chat_with_fallback", side_effect=LLMError("quota")):
        v = compare_one(CompareInput(case=_case(), actual=_actual_fail()), _opts())
    assert v.verdict == "inconclusive"
    assert v.verdict != "pass"  # 诚实降级：绝不假装通过
    assert v.rule_verdict == "fail"  # 保留规则判定
    assert "降级" in v.reason


def test_bad_json_degrades_to_inconclusive():
    with patch(
        "engine.comparator.compare.chat_with_fallback",
        return_value=_resp("这不是 JSON"),
    ):
        v = compare_one(CompareInput(case=_case(), actual=_actual_pass()), _opts())
    assert v.verdict == "inconclusive"
    assert v.rule_verdict == "pass"


def test_disabled_returns_rule_only_no_llm_call():
    with patch("engine.comparator.compare.chat_with_fallback") as mock_llm:
        v = compare_one(
            CompareInput(case=_case(), actual=_actual_fail()),
            ComparatorOptions(enabled=False, api_key="k", base_url="https://x"),
        )
    mock_llm.assert_not_called()
    assert v.verdict == "fail"
    assert v.rule_verdict == "fail"


def test_no_credentials_config_returns_rule_only():
    with patch("engine.comparator.compare.chat_with_fallback") as mock_llm:
        v = compare_one(
            CompareInput(case=_case(), actual=_actual_pass()),
            ComparatorOptions(enabled=True, api_key="", base_url=""),
        )
    mock_llm.assert_not_called()
    assert v.verdict == "pass"


def test_batch_returns_all_verdicts():
    with patch("engine.comparator.compare.chat_with_fallback", return_value=_resp(_GOOD)):
        vs = compare_batch(
            [
                CompareInput(case=_case(), actual=_actual_pass()),
                CompareInput(case=_case(), actual=_actual_fail()),
            ],
            _opts(),
        )
    assert len(vs) == 2
    assert vs[0].rule_verdict == "pass"
    assert vs[1].rule_verdict == "fail"


def test_redact_masks_sensitive_values():
    s = '{"password":"hunter2","token":"abc.def","note":"ok"}'
    out = redact(s)
    assert "hunter2" not in out
    assert "abc.def" not in out
    assert "password" in out
    assert "note" in out
