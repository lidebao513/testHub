"""对话代理（P1-2）单元测试：模板渲染 / 条件显隐 / 校验 / 文本兜底 / 凭证掩码。

全部为纯逻辑、无网络、无 LLM（LLM 精修默认关闭，不影响规则结果）。
"""

from __future__ import annotations

import sys
from pathlib import Path


# 让测试在「仓库根」可 import（pytest 根目录 conftest 已处理时此段为安全网）
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.dialogue import DialogueAgent, DialoguePlan, default_dialogue_template


def test_template_text_render_contains_core_fields() -> None:
    txt = default_dialogue_template().render_template_text()
    assert "source_kind" in txt and "mode" in txt and "scopes" in txt
    assert "test_url" in txt and "repo_url" in txt and "base" in txt


def test_template_json_schema_structure() -> None:
    js = default_dialogue_template().render_template_json()
    assert js["source_kinds"] == ["code", "url", "code+url"]
    assert "正常" in js["all_scopes"]
    assert len(js["fields"]) >= 10
    # 每个字段要么始终可见，要么带可见性规则
    for f in js["fields"]:
        assert (f["visible_when"] is None) or ("field" in f["visible_when"])


def test_visibility_url_vs_code() -> None:
    tpl = default_dialogue_template()
    vis_code = set(tpl.visible_fields({"source_kind": "code", "mode": "full"}))
    assert "test_url" not in vis_code and "local_path" in vis_code and "repo_url" in vis_code
    vis_url = set(tpl.visible_fields({"source_kind": "url", "mode": "full"}))
    assert "test_url" in vis_url and "local_path" not in vis_url and "repo_url" not in vis_url


def test_visibility_incremental() -> None:
    tpl = default_dialogue_template()
    vis_inc = set(tpl.visible_fields({"source_kind": "code", "mode": "incremental"}))
    assert "base" in vis_inc and "target" in vis_inc
    vis_full = set(tpl.visible_fields({"source_kind": "code", "mode": "full"}))
    assert "base" not in vis_full


def test_validate_success_code_plus_url() -> None:
    agent = DialogueAgent()
    plan = agent.handle_template(
        {
            "source_kind": "code+url",
            "mode": "full",
            "scopes": ["正常", "安全"],
            "repo_url": "https://github.com/x/y.git",
            "test_url": "http://21.163.93.12:8099",
            "login_user": "admin",
            "login_password": "secret",
            "login_otp": "123456",
        }
    )
    assert isinstance(plan, DialoguePlan)
    assert plan.ok, plan.errors
    assert plan.params["test_url"] == "http://21.163.93.12:8099"
    assert plan.params["repo_url"] == "https://github.com/x/y.git"
    # 生成-only 红线
    assert plan.params["execute"] is False
    # 凭证掩码
    assert plan.redacted["login_password"] == "***"
    assert plan.redacted["login_otp"] == "***"


def test_validate_missing_test_url_for_url_source() -> None:
    agent = DialogueAgent()
    plan = agent.handle_template({"source_kind": "url", "mode": "full"})
    assert not plan.ok
    assert any("test_url" in e for e in plan.errors)


def test_validate_missing_code_source() -> None:
    agent = DialogueAgent()
    plan = agent.handle_template({"source_kind": "code", "mode": "full"})
    assert not plan.ok
    assert any("repo_url" in e or "local_path" in e for e in plan.errors)


def test_validate_incremental_requires_base_target() -> None:
    agent = DialogueAgent()
    plan = agent.handle_template(
        {"source_kind": "code", "mode": "incremental", "repo_url": "x.git"}
    )
    assert not plan.ok
    assert any("base" in e for e in plan.errors) and any("target" in e for e in plan.errors)


def test_validate_invalid_scope_rejected() -> None:
    agent = DialogueAgent()
    plan = agent.handle_template(
        {
            "source_kind": "code",
            "mode": "full",
            "repo_url": "x.git",
            "scopes": ["不存在的维度"],
        }
    )
    assert not plan.ok
    assert any("非法行为维度" in e for e in plan.errors)


def test_default_scopes_applied_when_empty() -> None:
    agent = DialogueAgent()
    plan = agent.handle_template({"source_kind": "code", "repo_url": "x.git"})
    assert plan.ok
    # 默认覆盖系统 DEFAULT_SCOPE（含 正常/异常/安全/边界）
    assert set(plan.params["scopes"]) >= {"正常", "安全"}


def test_handle_text_url_with_credentials() -> None:
    agent = DialogueAgent()
    plan = agent.handle_text("被测地址 http://21.163.93.12:8099 账号 admin 密码 secret")
    assert plan.ok, plan.errors
    assert plan.intent == "text"
    assert plan.params["test_url"] == "http://21.163.93.12:8099"
    assert plan.params["login_user"] == "admin"
    assert plan.redacted["login_password"] == "***"


def test_handle_text_git_repo_as_code_source() -> None:
    agent = DialogueAgent()
    # 「仓库」别名会被 auto_input 收进 local_path，代理应校正回 repo_url
    plan = agent.handle_text("仓库 https://github.com/x/y.git 项目名 demo")
    assert plan.ok, plan.errors
    assert plan.params["repo_url"] == "https://github.com/x/y.git"
    assert plan.params["test_url"] == ""


def test_handle_text_unknown_source_degrades_honestly() -> None:
    agent = DialogueAgent()
    plan = agent.handle_text("请帮我生成测试用例")
    assert not plan.ok
    assert plan.intent == "unknown"
    assert plan.params == {}
    assert any("来源" in e for e in plan.errors)


def test_invalid_source_kind_rejected() -> None:
    agent = DialogueAgent()
    plan = agent.handle_template({"source_kind": "ftp", "mode": "full"})
    assert not plan.ok
    assert any("source_kind" in e for e in plan.errors)
