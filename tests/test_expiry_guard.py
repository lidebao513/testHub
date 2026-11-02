"""Django 侧：到期精度闸 + 免费额度耗尽一等信号 的单元测试。

与 `services/testgen/tests/test_expiry_guard.py` 同构（两侧 venv 隔离，各自一份）。
额外覆盖一处历史缺陷：`_as_bool` 曾把 `0/off` 当成默认值 True，导致 Django 侧
护栏「无法通过环境变量关闭」且与 sidecar 行为不一致。
"""

import json
import types
from datetime import date, datetime, timedelta

import pytest

from apps.requirement_analysis import llm_budget


def _msgs():
    return [{"role": "user", "content": "hi"}]


def _at(minutes: float) -> str:
    return (datetime.now() + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M")


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_BUDGET_ENFORCE", "on")
    monkeypatch.setenv("LLM_MODEL_ALLOWLIST", "")
    monkeypatch.setenv("LLM_USAGE_PATH", str(tmp_path / "usage.json"))
    monkeypatch.setenv("LLM_QUOTA_STATE_PATH", str(tmp_path / "quota.json"))
    monkeypatch.setenv("LLM_REGISTRY_BASELINE_PATH", str(tmp_path / "baseline.json"))
    monkeypatch.setenv("LLM_EXPIRY_GUARD_MINUTES", "60")
    return tmp_path


# ---------------------------------------------------------------------------
# 0. 历史缺陷：布尔解析
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["0", "off", "false", "no", "n", "f"])
def test_as_bool_false_values(monkeypatch, raw):
    """`0/off/no/false` 必须解析为 False，否则 LLM_BUDGET_ENFORCE=0 关不掉护栏。"""
    monkeypatch.setenv("LLM_BUDGET_ENFORCE", raw)
    assert llm_budget.load_settings().enforce is False


@pytest.mark.parametrize("raw", ["1", "on", "true", "yes"])
def test_as_bool_true_values(monkeypatch, raw):
    monkeypatch.setenv("LLM_BUDGET_ENFORCE", raw)
    assert llm_budget.load_settings().enforce is True


# ---------------------------------------------------------------------------
# 1. 到期时刻解析
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-11-13", "2026-11-13 23:59:59"),
        ("2026/11/13", "2026-11-13 23:59:59"),
        ("2026-11-13 08:30", "2026-11-13 08:30:00"),
        ("2026-11-13T08:30:00", "2026-11-13 08:30:00"),
        ("2026-11-13T08:30:00Z", "2026-11-13 08:30:00"),
        ("2026-11-13T08:30:00+08:00", "2026-11-13 08:30:00"),  # 剥时区偏移
    ],
)
def test_parse_expire_at_variants(raw, expected):
    at = llm_budget._parse_expire_at(raw)
    assert at is not None
    assert at.strftime("%Y-%m-%d %H:%M:%S") == expected


@pytest.mark.parametrize(
    "raw", ["2026-13-45", "2026-11-13 25:99", "下个月底", "", None]
)
def test_parse_expire_at_invalid(raw):
    assert llm_budget._parse_expire_at(raw) is None


def test_invalid_expiry_fails_closed(env, monkeypatch):
    monkeypatch.setenv("LLM_MODEL_REGISTRY", "bad|llm|2026-11-13 25:99|on")
    d = llm_budget.check_call("bad", _msgs(), cfg=llm_budget.load_settings())
    assert d.allowed is False
    assert "无法解析" in d.reason


# ---------------------------------------------------------------------------
# 2. 临近到期闸
# ---------------------------------------------------------------------------


def test_remaining_under_guard_blocks(env, monkeypatch):
    monkeypatch.setenv("LLM_MODEL_REGISTRY", f"soon|llm|{_at(30)}|on")
    d = llm_budget.check_call("soon", _msgs(), cfg=llm_budget.load_settings())
    assert d.allowed is False
    assert "提前禁用闸" in d.reason


def test_remaining_above_guard_allows(env, monkeypatch):
    monkeypatch.setenv("LLM_MODEL_REGISTRY", f"later|llm|{_at(120)}|on")
    assert (
        llm_budget.check_call("later", _msgs(), cfg=llm_budget.load_settings()).allowed
        is True
    )


def test_guard_zero_disables_gate(env, monkeypatch):
    monkeypatch.setenv("LLM_EXPIRY_GUARD_MINUTES", "0")
    monkeypatch.setenv("LLM_MODEL_REGISTRY", f"soon|llm|{_at(30)}|on")
    assert (
        llm_budget.check_call("soon", _msgs(), cfg=llm_budget.load_settings()).allowed
        is True
    )


def test_guard_zero_still_blocks_expired(env, monkeypatch):
    monkeypatch.setenv("LLM_EXPIRY_GUARD_MINUTES", "0")
    monkeypatch.setenv("LLM_MODEL_REGISTRY", f"gone|llm|{_at(-5)}|on")
    d = llm_budget.check_call("gone", _msgs(), cfg=llm_budget.load_settings())
    assert d.allowed is False
    assert "到期" in d.reason


def test_evaluate_registry_reports_near_expiry(env, monkeypatch):
    monkeypatch.setenv("LLM_MODEL_REGISTRY", f"soon|llm|{_at(30)}|on")
    v = llm_budget.evaluate_registry(llm_budget.load_settings())[0]
    assert v["near_expiry"] is True
    assert v["usable_for_text"] is False


def test_baseline_persist_failure_fails_closed(env, monkeypatch, tmp_path):
    """「长期」起算日落盘失败 → 必须 fail-closed（否则退回今天 = 永不过期）。"""
    monkeypatch.setenv("LLM_MODEL_REGISTRY_SNAPSHOT", "")
    monkeypatch.setenv("LLM_MODEL_REGISTRY", "forever|llm||on")
    blocker = tmp_path / "baseline_as_dir"
    blocker.mkdir()
    monkeypatch.setenv("LLM_REGISTRY_BASELINE_PATH", str(blocker))
    d = llm_budget.check_call("forever", _msgs(), cfg=llm_budget.load_settings())
    assert d.allowed is False
    assert "起算日无法确定" in d.reason


# ---------------------------------------------------------------------------
# 3. FreeTierOnly 一等信号
# ---------------------------------------------------------------------------


class _HttpxLike(Exception):
    """模拟 httpx.HTTPStatusError：body 在 response.text 里，无 status_code 属性。"""

    def __init__(self):
        super().__init__("Client error '403 Forbidden' for url 'https://x'")
        self.response = types.SimpleNamespace(
            status_code=403,
            text='{"code":"AllocationQuota.FreeTierOnly","message":"Free quota exhausted."}',
        )


class _RateLimited(Exception):
    def __init__(self):
        super().__init__("rate limit exceeded")
        self.response = types.SimpleNamespace(
            status_code=429, text='{"code":"Throttling"}'
        )


def test_is_free_tier_exhausted_httpx_shape():
    assert llm_budget.is_free_tier_exhausted(_HttpxLike()) is True


def test_rate_limit_not_exhausted():
    assert llm_budget.is_free_tier_exhausted(_RateLimited()) is False


def test_mark_then_block(env):
    llm_budget.mark_quota_exhausted("free-a", message="Free quota exhausted.")
    d = llm_budget.check_call("free-a", _msgs(), cfg=llm_budget.load_settings())
    assert d.allowed is False
    assert "免费额度已耗尽" in d.reason
    assert d.scope == "model"


def test_quota_status_three_states(env):
    assert llm_budget.quota_status("never-called") == "unknown"
    llm_budget.record_usage("called-ok", 5, 5)
    assert llm_budget.quota_status("called-ok") == "ok"
    llm_budget.mark_quota_exhausted("dead")
    assert llm_budget.quota_status("dead") == "exhausted"


def test_marks_clear(env):
    llm_budget.mark_quota_exhausted("a")
    llm_budget.mark_quota_exhausted("b")
    assert llm_budget.clear_quota_marks("a") == 1
    assert llm_budget.clear_quota_marks() == 1
    assert llm_budget._quota_marks() == {}


def test_mark_file_is_json(env):
    llm_budget.mark_quota_exhausted("a", message="x")
    data = json.loads(llm_budget._quota_state_path().read_text(encoding="utf-8"))
    assert data["models"]["a"]["state"] == "exhausted"


# ---------------------------------------------------------------------------
# 4. 到期升序
# ---------------------------------------------------------------------------


def test_sort_chain_by_expiry(env, monkeypatch):
    monkeypatch.setenv(
        "LLM_MODEL_REGISTRY",
        f"late|llm|{(date.today() + timedelta(days=40)).isoformat()}|on,"
        f"soon|llm|{(date.today() + timedelta(days=5)).isoformat()}|on,"
        f"mid|llm|{(date.today() + timedelta(days=20)).isoformat()}|on",
    )
    assert llm_budget.sort_chain_by_expiry(["late", "soon", "mid"]) == [
        "soon",
        "mid",
        "late",
    ]


def test_sort_chain_unregistered_last(env, monkeypatch):
    monkeypatch.setenv(
        "LLM_MODEL_REGISTRY",
        f"known|llm|{(date.today() + timedelta(days=3)).isoformat()}|on",
    )
    assert llm_budget.sort_chain_by_expiry(["x", "known", "y"]) == ["known", "x", "y"]
