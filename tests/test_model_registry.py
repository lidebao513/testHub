"""Django 侧模型登记册（LLM_MODEL_REGISTRY）单元测试：类型/到期/开启三道闸 + 判定清单。

与 `services/testgen/core/llm_budget.py` 同构（两侧 venv 隔离，无法共享代码）。
不依赖数据库，只验证护栏准入逻辑与 evaluate_registry 的判定口径。
"""
import json
from datetime import date, timedelta

import pytest

from apps.requirement_analysis import llm_budget

REGISTRY = (
    # 相对日期，避免到期后测试自然失败的时间炸弹
    f"deepseek-v4-pro-0813|llm|{(date.today() + timedelta(days=11)).isoformat()}|on,"
    f"glm-5.3|llm|{(date.today() + timedelta(days=21)).isoformat()}|on,"
    f"deepseek-v4.1-flash|llm|{(date.today() + timedelta(days=41)).isoformat()}|on,"
    f"qwen-image-3.0|image|{(date.today() + timedelta(days=1)).isoformat()}|on,"
    f"wan3.0-video|video|{(date.today() + timedelta(days=3)).isoformat()}|on,"
    f"off-model|llm|{(date.today() + timedelta(days=300)).isoformat()}|off,"
    "expired-model|llm|2000-01-01|on"
)


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setenv("LLM_BUDGET_ENFORCE", "on")
    monkeypatch.setenv("LLM_MODEL_ALLOWLIST", "")
    monkeypatch.setenv("LLM_MODEL_REGISTRY", REGISTRY)
    return llm_budget.load_settings()


def test_text_models_allowed(cfg):
    msgs = [{"role": "user", "content": "hi"}]
    for m in ("deepseek-v4-pro-0813", "glm-5.3", "deepseek-v4.1-flash"):
        d = llm_budget.check_call(m, msgs, cfg=cfg)
        assert d.allowed, d.reason


def test_vision_model_rejected_by_type(cfg):
    d = llm_budget.check_call("qwen-image-3.0", [{"role": "user", "content": "x"}], cfg=cfg)
    assert not d.allowed
    assert "非文本对话模型" in d.reason
    assert d.scope == "model"


def test_video_model_rejected_by_type(cfg):
    d = llm_budget.check_call("wan3.0-video", [{"role": "user", "content": "x"}], cfg=cfg)
    assert not d.allowed
    assert "非文本对话模型" in d.reason


def test_disabled_and_expired_rejected(cfg):
    assert not llm_budget.check_call("off-model", [{"role": "user", "content": "x"}], cfg=cfg).allowed
    assert not llm_budget.check_call("expired-model", [{"role": "user", "content": "x"}], cfg=cfg).allowed


def test_registry_absent_is_noop(monkeypatch):
    monkeypatch.setenv("LLM_BUDGET_ENFORCE", "on")
    monkeypatch.setenv("LLM_MODEL_ALLOWLIST", "")
    monkeypatch.delenv("LLM_MODEL_REGISTRY", raising=False)
    cfg = llm_budget.load_settings()
    d = llm_budget.check_call("some-random-model", [{"role": "user", "content": "x"}], cfg=cfg)
    assert d.allowed


def test_evaluate_registry_verdict(cfg):
    v = {row["model"]: row for row in llm_budget.evaluate_registry(cfg)}
    assert v["deepseek-v4-pro-0813"]["usable_for_text"] is True
    assert v["qwen-image-3.0"]["usable_for_text"] is False
    assert v["qwen-image-3.0"]["text_capable"] is False
    assert v["off-model"]["enabled"] is False
    assert v["expired-model"]["expired"] is True
    usable = [r["model"] for r in llm_budget.evaluate_registry(cfg) if r["usable_for_text"]]
    assert usable == ["deepseek-v4-pro-0813", "glm-5.3", "deepseek-v4.1-flash"]


def test_resolve_text_chain_orders_by_expiry(cfg):
    assert llm_budget.resolve_text_chain(cfg) == [
        "deepseek-v4-pro-0813",
        "glm-5.3",
        "deepseek-v4.1-flash",
    ]


def test_expiry_day_itself_still_usable(monkeypatch):
    """到期当天仍可用（截图口径「剩余 N 天」），次日才判过期。"""
    today = date.today()
    monkeypatch.setenv("LLM_BUDGET_ENFORCE", "on")
    monkeypatch.setenv("LLM_MODEL_ALLOWLIST", "")
    monkeypatch.setenv(
        "LLM_MODEL_REGISTRY",
        f"today-model|llm|{today.isoformat()}|on,"
        f"yesterday-model|llm|{(today - timedelta(days=1)).isoformat()}|on",
    )
    cfg = llm_budget.load_settings()
    msgs = [{"role": "user", "content": "x"}]
    assert llm_budget.check_call("today-model", msgs, cfg=cfg).allowed is True
    assert llm_budget.check_call("yesterday-model", msgs, cfg=cfg).allowed is False


def test_invalid_expiry_fails_closed(monkeypatch):
    """日期写错必须拒绝（fail-closed），不能被当成「永不过期」。"""
    monkeypatch.setenv("LLM_BUDGET_ENFORCE", "on")
    monkeypatch.setenv("LLM_MODEL_ALLOWLIST", "")
    monkeypatch.setenv(
        "LLM_MODEL_REGISTRY",
        "typo-model|llm|下个月底|on,bad-date|llm|2026-13-45|on",
    )
    cfg = llm_budget.load_settings()
    msgs = [{"role": "user", "content": "x"}]
    for m in ("typo-model", "bad-date"):
        d = llm_budget.check_call(m, msgs, cfg=cfg)
        assert d.allowed is False, m
        assert "无法解析" in d.reason


def test_empty_expiry_defaults_to_one_month(monkeypatch, tmp_path):
    """截图写「长期」→ 按「登记日 + 30 天」算，登记当天可用。"""
    monkeypatch.setenv("LLM_BUDGET_ENFORCE", "on")
    monkeypatch.setenv("LLM_MODEL_ALLOWLIST", "")
    monkeypatch.setenv("LLM_MODEL_REGISTRY", "forever|llm||on")
    monkeypatch.setenv("LLM_REGISTRY_BASELINE_PATH", str(tmp_path / "baseline.json"))
    snapshot = date.today()
    monkeypatch.setenv("LLM_MODEL_REGISTRY_SNAPSHOT", snapshot.isoformat())
    cfg = llm_budget.load_settings()
    assert llm_budget.check_call("forever", [{"role": "user", "content": "x"}], cfg=cfg).allowed is True
    v = {r["model"]: r for r in llm_budget.evaluate_registry(cfg)}["forever"]
    assert v["expiry"] == (snapshot + timedelta(days=30)).isoformat()
    assert v["expiry_assumed"] is True


def test_empty_expiry_expires_one_month_after_registration(monkeypatch, tmp_path):
    """登记日已是 31 天前 + 到期留空 → 必须判过期（证明不是「每天 +30 天」滚动）。"""
    monkeypatch.setenv("LLM_BUDGET_ENFORCE", "on")
    monkeypatch.setenv("LLM_MODEL_ALLOWLIST", "")
    monkeypatch.setenv("LLM_MODEL_REGISTRY", "forever|llm||on")
    monkeypatch.setenv("LLM_REGISTRY_BASELINE_PATH", str(tmp_path / "baseline.json"))
    monkeypatch.setenv(
        "LLM_MODEL_REGISTRY_SNAPSHOT", (date.today() - timedelta(days=31)).isoformat()
    )
    cfg = llm_budget.load_settings()
    d = llm_budget.check_call("forever", [{"role": "user", "content": "x"}], cfg=cfg)
    assert d.allowed is False
    assert "默认" in d.reason and "有效期" in d.reason


def test_empty_expiry_baseline_persisted_without_snapshot(monkeypatch, tmp_path):
    """未配登记日时用「首次登记日」落盘，之后不随日期漂移。"""
    monkeypatch.setenv("LLM_BUDGET_ENFORCE", "on")
    monkeypatch.setenv("LLM_MODEL_ALLOWLIST", "")
    monkeypatch.setenv("LLM_MODEL_REGISTRY", "forever|llm||on")
    monkeypatch.setenv("LLM_MODEL_REGISTRY_SNAPSHOT", "")
    base = tmp_path / "baseline.json"
    monkeypatch.setenv("LLM_REGISTRY_BASELINE_PATH", str(base))
    cfg = llm_budget.load_settings()
    msgs = [{"role": "user", "content": "x"}]
    assert llm_budget.check_call("forever", msgs, cfg=cfg).allowed is True
    first = json.loads(base.read_text(encoding="utf-8"))["forever"]
    llm_budget.check_call("forever", msgs, cfg=cfg)
    assert json.loads(base.read_text(encoding="utf-8"))["forever"] == first


def test_multimodal_allowed_realtime_rejected(monkeypatch):
    """多模态理解型（omni）可文本输出→放行；realtime 走 websocket→拒绝。"""
    monkeypatch.setenv("LLM_BUDGET_ENFORCE", "on")
    monkeypatch.setenv("LLM_MODEL_ALLOWLIST", "")
    monkeypatch.setenv(
        "LLM_MODEL_REGISTRY",
        "qwen3.8-omni-flash|multimodal|2027-12-17|on,"
        "qwen3.8-omni-flash-realtime|realtime|2027-12-21|on",
    )
    cfg = llm_budget.load_settings()
    msgs = [{"role": "user", "content": "x"}]
    assert llm_budget.check_call("qwen3.8-omni-flash", msgs, cfg=cfg).allowed is True
    rt = llm_budget.check_call("qwen3.8-omni-flash-realtime", msgs, cfg=cfg)
    assert rt.allowed is False
    assert "realtime" in rt.reason


def test_days_left_and_expiry_normalized(monkeypatch):
    days = 19
    exp = date.today() + timedelta(days=days)
    slash = f"{exp.year}/{exp.month:02d}/{exp.day:02d}"
    monkeypatch.setenv("LLM_MODEL_REGISTRY", f"m|llm|{slash}|on")
    v = llm_budget.evaluate_registry(llm_budget.load_settings())[0]
    assert v["days_left"] == days
    assert v["expiry"] == exp.isoformat()
    assert v["usable_for_text"] is True


# ---------------------------------------------------------------------------
# 重复条目：换批次录入时「旧的置 off 条目」不能把「新录入的 on 条目」顶掉
# ---------------------------------------------------------------------------


def test_duplicate_entry_off_then_on_keeps_enabled(monkeypatch):
    """旧 off 条目排在新 on 条目**前面**时，后者应生效。"""
    monkeypatch.setenv(
        "LLM_MODEL_REGISTRY", "dup-a|llm|2999-12-31|off,dup-a|llm|2999-12-31|on"
    )
    cfg = llm_budget.load_settings()
    assert llm_budget.check_call("dup-a", [{"role": "user", "content": "x"}], cfg=cfg).allowed


def test_duplicate_entry_on_then_off_keeps_enabled(monkeypatch):
    """反过来（on 在前、旧 off 在后）同样要保留 on——不能靠书写顺序。"""
    monkeypatch.setenv(
        "LLM_MODEL_REGISTRY", "dup-b|llm|2999-12-31|on,dup-b|llm|2999-12-31|off"
    )
    cfg = llm_budget.load_settings()
    assert llm_budget.check_call("dup-b", [{"role": "user", "content": "x"}], cfg=cfg).allowed


def test_duplicate_entry_all_off_stays_off(monkeypatch):
    """全部都是 off 时仍然是 off（去重不能把关闭的模型"救活"）。"""
    monkeypatch.setenv(
        "LLM_MODEL_REGISTRY", "dup-c|llm|2999-12-31|off,dup-c|llm|2999-12-31|off"
    )
    cfg = llm_budget.load_settings()
    assert not llm_budget.check_call(
        "dup-c", [{"role": "user", "content": "x"}], cfg=cfg
    ).allowed
