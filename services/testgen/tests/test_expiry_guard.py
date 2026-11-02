"""到期精度闸 + 免费额度耗尽一等信号 的单元测试。

覆盖四件事：
1. **到期时刻精度**：`YYYY-MM-DD` 按当日 23:59:59 失效；`YYYY-MM-DD HH:MM` 精确到分钟；
   非法写法 fail-closed；
2. **临近到期闸**：剩余 < `LLM_EXPIRY_GUARD_MINUTES`（默认 60）即提前禁用，0 = 关闭；
3. **FreeTierOnly 一等信号**：403 落盘标记 → 后续调用本地跳过 → 自动降级到下一个；
4. **到期升序**：降级链按到期时刻升序，快到期的先消耗。
"""

import os
import sys
import types
from datetime import date, datetime, timedelta
from typing import ClassVar
from unittest.mock import patch

import pytest


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import llm_budget
from core.llm_budget import (
    check_call,
    clear_quota_marks,
    evaluate_registry,
    is_free_tier_exhausted,
    load_settings,
    mark_quota_exhausted,
    quota_status,
    sort_chain_by_expiry,
)
from engine import llm_fallback


def _msgs():
    return [{"role": "user", "content": "hi"}]


def _at(minutes: float) -> str:
    """当前时间 + N 分钟的 `YYYY-MM-DD HH:MM` 字符串。"""
    return (datetime.now() + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M")


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """隔离环境：用量/额度标记/基准日都落临时目录，护栏开启、白名单清空。"""
    monkeypatch.setenv("LLM_BUDGET_ENFORCE", "on")
    monkeypatch.setenv("LLM_MODEL_ALLOWLIST", "")
    monkeypatch.setenv("LLM_USAGE_PATH", str(tmp_path / "usage.json"))
    monkeypatch.setenv("LLM_QUOTA_STATE_PATH", str(tmp_path / "quota.json"))
    monkeypatch.setenv("LLM_REGISTRY_BASELINE_PATH", str(tmp_path / "baseline.json"))
    monkeypatch.setenv("LLM_EXPIRY_GUARD_MINUTES", "60")
    llm_fallback.reset_fallback_cache()
    return tmp_path


# ---------------------------------------------------------------------------
# 1. 到期时刻解析
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-11-13", "2026-11-13 23:59:59"),  # 只写日期 → 当日末刻
        ("2026/11/13", "2026-11-13 23:59:59"),  # 斜杠归一化
        ("2026-11-13 08:30", "2026-11-13 08:30:00"),  # 精确到分钟
        ("2026-11-13 08:30:15", "2026-11-13 08:30:15"),
        ("2026-11-13T08:30:00", "2026-11-13 08:30:00"),
        ("2026-11-13T08:30:00Z", "2026-11-13 08:30:00"),  # 去 Z 后缀
        ("2026-11-13T08:30:00+08:00", "2026-11-13 08:30:00"),  # 剥时区偏移，按本地时间
        ("2026-11-13 08:30:00-05:00", "2026-11-13 08:30:00"),
    ],
)
def test_parse_expire_at_variants(raw, expected):
    at = llm_budget._parse_expire_at(raw)
    assert at is not None
    assert at.strftime("%Y-%m-%d %H:%M:%S") == expected


@pytest.mark.parametrize("raw", ["2026-13-45", "2026-11-13 25:99", "下个月底", "", None])
def test_parse_expire_at_invalid_returns_none(raw):
    """非法/空一律返回 None——不做「截前 10 字符」兜底（那是 fail-open，会静默放行）。"""
    assert llm_budget._parse_expire_at(raw) is None


def test_invalid_expiry_rejected_fail_closed(env, monkeypatch):
    monkeypatch.setenv("LLM_MODEL_REGISTRY", "bad|llm|2026-11-13 25:99|on")
    d = check_call("bad", _msgs(), cfg=load_settings())
    assert d.allowed is False
    assert "无法解析" in d.reason


# ---------------------------------------------------------------------------
# 2. 临近到期闸
# ---------------------------------------------------------------------------


def test_remaining_under_guard_blocks(env, monkeypatch):
    """剩余 30 分钟（< 60 分钟闸）→ 提前禁用，理由要说清是「闸」而不是「已过期」。"""
    monkeypatch.setenv("LLM_MODEL_REGISTRY", f"soon|llm|{_at(30)}|on")
    d = check_call("soon", _msgs(), cfg=load_settings())
    assert d.allowed is False
    assert "提前禁用闸" in d.reason
    assert d.scope == "model"  # 仅跳过该模型，可继续用链上下一个


def test_remaining_above_guard_allows(env, monkeypatch):
    """剩余 2 小时（> 60 分钟闸）→ 正常放行。"""
    monkeypatch.setenv("LLM_MODEL_REGISTRY", f"later|llm|{_at(120)}|on")
    d = check_call("later", _msgs(), cfg=load_settings())
    assert d.allowed is True, d.reason


def test_guard_zero_disables_gate(env, monkeypatch):
    """闸值 0 = 关闭提前禁用；剩 30 分钟仍放行。"""
    monkeypatch.setenv("LLM_EXPIRY_GUARD_MINUTES", "0")
    monkeypatch.setenv("LLM_MODEL_REGISTRY", f"soon|llm|{_at(30)}|on")
    assert check_call("soon", _msgs(), cfg=load_settings()).allowed is True


def test_guard_zero_still_blocks_expired(env, monkeypatch):
    """闸值 0 只关「提前禁用」，已过期的模型仍必须拒绝。"""
    monkeypatch.setenv("LLM_EXPIRY_GUARD_MINUTES", "0")
    monkeypatch.setenv("LLM_MODEL_REGISTRY", f"gone|llm|{_at(-5)}|on")
    d = check_call("gone", _msgs(), cfg=load_settings())
    assert d.allowed is False
    assert "到期" in d.reason


def test_guard_minutes_configurable(env, monkeypatch):
    """闸值可调：设 180 分钟后，剩 2 小时的模型也应被禁用。"""
    monkeypatch.setenv("LLM_EXPIRY_GUARD_MINUTES", "180")
    monkeypatch.setenv("LLM_MODEL_REGISTRY", f"later|llm|{_at(120)}|on")
    d = check_call("later", _msgs(), cfg=load_settings())
    assert d.allowed is False
    assert "提前禁用闸" in d.reason


def test_baseline_persist_failure_fails_closed(env, monkeypatch, tmp_path):
    """「长期」起算日落盘失败 → 必须 fail-closed。

    这是第二轮审计发现的漏洞：原实现落盘失败会退回「今天」，下次又「今天 + 30 天」，
    等于**永不过期**——正是这套护栏要防的情况。这里用「把基准日路径指向目录」制造写入失败。
    """
    monkeypatch.setenv("LLM_MODEL_REGISTRY_SNAPSHOT", "")  # 不配登记日，走落盘推定
    monkeypatch.setenv("LLM_MODEL_REGISTRY", "forever|llm||on")
    blocker = tmp_path / "baseline_as_dir"
    blocker.mkdir()
    monkeypatch.setenv("LLM_REGISTRY_BASELINE_PATH", str(blocker))
    d = check_call("forever", _msgs(), cfg=load_settings())
    assert d.allowed is False, "落盘失败时不能放行（否则等于永不过期）"
    assert "起算日无法确定" in d.reason


def test_evaluate_registry_reports_near_expiry(env, monkeypatch):
    monkeypatch.setenv("LLM_MODEL_REGISTRY", f"soon|llm|{_at(30)}|on")
    v = evaluate_registry(load_settings())[0]
    assert v["near_expiry"] is True
    assert v["usable_for_text"] is False
    assert v["minutes_left"] is not None and v["minutes_left"] < 60


# ---------------------------------------------------------------------------
# 3. FreeTierOnly 一等信号
# ---------------------------------------------------------------------------


class _QuotaExhausted(Exception):
    """模拟 openai SDK 的 403 PermissionDeniedError（FreeTierOnly）。"""

    status_code = 403
    code = "AllocationQuota.FreeTierOnly"
    message = (
        "Free quota exhausted. To continue accessing the model on a paid basis, "
        'please add funds or disable the "use free tier only" mode in the management console.'
    )
    body: ClassVar[dict] = {"code": "AllocationQuota.FreeTierOnly"}


class _RateLimited(Exception):
    status_code = 429
    message = "Requests rate limit exceeded"


class _HttpxLike(Exception):
    """模拟 httpx.HTTPStatusError：body 在 response.text 里，无 status_code 属性。"""

    def __init__(self):
        super().__init__("Client error '403 Forbidden' for url 'https://x'")
        self.response = types.SimpleNamespace(
            status_code=403,
            text='{"code":"AllocationQuota.FreeTierOnly","message":"Free quota exhausted."}',
        )


def test_is_free_tier_exhausted_openai_shape():
    assert is_free_tier_exhausted(_QuotaExhausted()) is True


def test_is_free_tier_exhausted_httpx_shape():
    assert is_free_tier_exhausted(_HttpxLike()) is True


def test_rate_limit_not_treated_as_exhausted():
    """429 是可恢复限流，绝不能当成「额度已耗尽」永久标记（会误杀好模型）。"""
    assert is_free_tier_exhausted(_RateLimited()) is False


def test_mark_then_block(env):
    """标记后 check_call 本地直接拒绝，不再消耗一次真实请求。"""
    mark_quota_exhausted("free-a", message="Free quota exhausted.")
    d = check_call("free-a", _msgs(), cfg=load_settings())
    assert d.allowed is False
    assert "免费额度已耗尽" in d.reason
    assert d.scope == "model"


def test_quota_status_three_states(env):
    """三态：exhausted（有标记）/ ok（有成功记账）/ unknown（都没）。"""
    assert quota_status("never-called") == "unknown"
    llm_budget.record_usage("called-ok", 5, 5)
    assert quota_status("called-ok") == "ok"
    mark_quota_exhausted("dead")
    assert quota_status("dead") == "exhausted"
    # 已标记的不被「曾成功」覆盖
    llm_budget.record_usage("dead", 5, 5)
    assert quota_status("dead") == "exhausted"


def test_marks_persist_and_clear(env):
    mark_quota_exhausted("a")
    mark_quota_exhausted("b")
    assert set(llm_budget._quota_marks()) == {"a", "b"}
    assert clear_quota_marks("a") == 1
    assert set(llm_budget._quota_marks()) == {"b"}
    assert clear_quota_marks() == 1
    assert llm_budget._quota_marks() == {}


def test_quota_mark_ttl_expires(env, monkeypatch):
    """设了 TTL 后，超期的标记自动失效（用于「平台补发额度」后无需手工清除）。"""
    mark_quota_exhausted("a")
    path = llm_budget._quota_state_path()
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    data["models"]["a"]["at"] = (datetime.now() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("LLM_QUOTA_MARKER_TTL_HOURS", "24")
    assert quota_status("a") == "unknown"


# ---------------------------------------------------------------------------
# 4. 到期升序 + 运行期降级
# ---------------------------------------------------------------------------


def test_sort_chain_by_expiry_soonest_first(env, monkeypatch):
    monkeypatch.setenv(
        "LLM_MODEL_REGISTRY",
        f"late|llm|{(date.today() + timedelta(days=40)).isoformat()}|on,"
        f"soon|llm|{(date.today() + timedelta(days=5)).isoformat()}|on,"
        f"mid|llm|{(date.today() + timedelta(days=20)).isoformat()}|on",
    )
    assert sort_chain_by_expiry(["late", "soon", "mid"]) == ["soon", "mid", "late"]


def test_sort_chain_keeps_unregistered_last(env, monkeypatch):
    """未登记模型无法判到期 → 排最后，并保持它们之间的原有相对顺序。"""
    monkeypatch.setenv(
        "LLM_MODEL_REGISTRY",
        f"known|llm|{(date.today() + timedelta(days=3)).isoformat()}|on",
    )
    assert sort_chain_by_expiry(["x-unknown", "known", "y-unknown"]) == [
        "known",
        "x-unknown",
        "y-unknown",
    ]


class _Choice:
    message = types.SimpleNamespace(content="ok")


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5


class _Resp:
    choices: ClassVar[list] = [_Choice()]
    usage = _Usage()


def _client(handler):
    return types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=handler))
    )


def test_fallback_skips_marked_exhausted_model(env):
    """已标记耗尽的模型在本地被跳过，只对下一个模型发请求。"""
    mark_quota_exhausted("free-a")
    seen: list[str] = []

    def handler(model, messages, temperature, **kwargs):
        seen.append(model)
        return _Resp()

    with patch("openai.OpenAI", return_value=_client(handler)):
        llm_fallback.chat_with_fallback(
            channel="llm",
            api_key="k",
            base_url="https://x",
            timeout=10,
            model_chain=["free-a", "free-b"],
            messages=_msgs(),
        )
    assert seen == ["free-b"], "已耗尽的模型不该真的发请求"


def test_fallback_marks_exhausted_on_403_then_degrades(env):
    """链头返回 403 FreeTierOnly → 落盘标记 + 自动降级到下一个模型。"""
    seen: list[str] = []

    def handler(model, messages, temperature, **kwargs):
        seen.append(model)
        if model == "free-a":
            raise _QuotaExhausted()
        return _Resp()

    with patch("openai.OpenAI", return_value=_client(handler)):
        llm_fallback.chat_with_fallback(
            channel="llm",
            api_key="k",
            base_url="https://x",
            timeout=10,
            model_chain=["free-a", "free-b"],
            messages=_msgs(),
        )
    assert seen == ["free-a", "free-b"], "撞到 403 后必须继续试下一个模型"
    assert quota_status("free-a") == "exhausted", "403 必须落盘成耗尽标记"

    # 第二次调用：free-a 已在本地被跳过，不会再发请求
    seen.clear()
    with patch("openai.OpenAI", return_value=_client(handler)):
        llm_fallback.chat_with_fallback(
            channel="llm",
            api_key="k",
            base_url="https://x",
            timeout=10,
            model_chain=["free-a", "free-b"],
            messages=_msgs(),
        )
    assert seen == ["free-b"], "标记后第二次调用不该再撞一次 403"


def test_fallback_skips_near_expiry_model(env, monkeypatch):
    """临近到期（剩 30 分钟）的模型在本地被跳过，改用链上仍有效的模型。"""
    monkeypatch.setenv(
        "LLM_MODEL_REGISTRY",
        f"soon|llm|{_at(30)}|on,later|llm|{(date.today() + timedelta(days=10)).isoformat()}|on",
    )
    seen: list[str] = []

    def handler(model, messages, temperature, **kwargs):
        seen.append(model)
        return _Resp()

    with patch("openai.OpenAI", return_value=_client(handler)):
        llm_fallback.chat_with_fallback(
            channel="llm",
            api_key="k",
            base_url="https://x",
            timeout=10,
            model_chain=["later", "soon"],  # 故意把 soon 写在后面，验证排序也会把它靠前并跳过
            messages=_msgs(),
        )
    assert seen == ["later"]
