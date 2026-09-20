"""A1 · 统一智能输入框解析器与「地址通道」入口的契约测试。

覆盖点（对应 `两条生成流程_链路梳理与补齐方案.md` 阶段一 A1 验收口径）：
- 混排文本能被切回结构化字段；URL 里的 `host:port` **不得**被误当成标签；
- 裸 token 按形态兜底（邮箱 / 密码特征 / 数字口令 / 已存在目录）；
- 识别不出的片段如实进 `unknown`，绝不静默丢弃；
- `redacted()` 是唯一允许外泄的视图，明文凭证不得出现在其中；
- 入口校验：既无代码目录又无被测地址 → 明确报错；只给地址也能产出用例。
"""

from __future__ import annotations

import json

from core.auto_input import (
    _LABEL_ALIASES,
    _LABEL_GROUPS,
    _SETTERS,
    _normalize_label,
    mask_secret,
    parse_auto_input,
)
from engine import pipeline


# ---------------------------------------------------------------- 解析：标签式
def test_labeled_mixed_text_is_split_into_fields():
    parsed = parse_auto_input(
        "地址: http://host:8090/chat 账号: test_ft001@ft.cntaiping.com "
        "密码: Tp0909@test 动态码: 260909"
    )
    assert parsed.url == "http://host:8090/chat"
    assert parsed.user == "test_ft001@ft.cntaiping.com"
    assert parsed.password == "Tp0909@test"
    assert parsed.otp == "260909"
    assert parsed.has_credentials()
    assert parsed.unknown == []


def test_url_host_port_is_not_mistaken_for_a_label():
    """`http://host:8090/x` 里的 `host:` 不能被当成标签（否则 url 会变成 `//8090/x`）。"""
    assert parse_auto_input("http://host:8090/chat").url == "http://host:8090/chat"
    assert parse_auto_input("地址=http://h:1/a").url == "http://h:1/a"


def test_login_page_url_is_split_into_origin_and_login_url():
    """登录页地址必须拆出源地址：入口若是 /login，抓到的每个路由都会落在登录页上。"""
    parsed = parse_auto_input("地址: http://a.b.c:9001/login")
    assert parsed.url == "http://a.b.c:9001"
    assert parsed.login_url == "http://a.b.c:9001/login"


def test_email_trailing_sentence_dot_is_trimmed_for_user_only():
    parsed = parse_auto_input("账号: someone@corp.com.")
    assert parsed.user == "someone@corp.com"
    # 密码里的点属合法字符，不得被顺手裁掉
    assert parse_auto_input("密码: Pass.word.1").password == "Pass.word.1"


def test_scopes_and_mode_are_normalized():
    parsed = parse_auto_input("范围: 正常+安全+边界  模式: 增量")
    assert parsed.scopes == ["正常", "安全", "边界"]
    assert parsed.mode == "incremental"


def test_unknown_mode_is_reported_not_guessed():
    parsed = parse_auto_input("模式: 随缘")
    assert parsed.mode == ""
    assert any("模式" in u for u in parsed.unknown)


# ---------------------------------------------------------------- 解析：裸串式
def test_bare_tokens_are_classified_by_shape():
    parsed = parse_auto_input("http://h:8090/chat me@corp.com Tp0909@test 260909")
    assert parsed.url == "http://h:8090/chat"
    assert parsed.user == "me@corp.com"
    assert parsed.password == "Tp0909@test"
    assert parsed.otp == "260909"


def test_bare_existing_directory_becomes_local_path(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert parse_auto_input(str(repo)).local_path == str(repo)


def test_windows_drive_letter_is_not_mistaken_for_a_label():
    """盘符 `C:\\code\\repo` 里的 `C:` 不得被当成标签——否则整条路径被吞进 unknown。

    这类输入在 Windows 上是常态，一旦误判，裸串路径识别会**整体失效**且没有任何告警。
    """
    raw = r"C:\code\repo"
    parsed = parse_auto_input(raw)
    assert not [u for u in parsed.unknown if u.startswith("C=")], "盘符被误判成标签"
    assert parsed.local_path == raw or raw in parsed.unknown


def test_unrecognized_fragments_are_reported():
    parsed = parse_auto_input("帮我跑一下这个")
    assert parsed.url == ""
    assert parsed.unknown, "未识别片段必须如实回报，不能静默丢弃"


def test_project_name_and_routes_labels_are_recognized():
    parsed = parse_auto_input("项目: 福享Agent 路由: /pc/tasks,/pc/email")
    assert parsed.project_name == "福享Agent"
    assert parsed.routes == ["/pc/tasks", "/pc/email"]


# ---------------------------------------------------------------- 解析：空格式（中文习惯）
def test_space_separated_labels_are_recognized():
    """中文习惯的混排：`账号 test004 密码 xxx`（**无分隔符**）也必须成链。

    这是最容易被漏掉的一种输入——漏了它，用户按习惯粘贴就被静默降级成匿名访问。
    """
    parsed = parse_auto_input(
        "https://example.com/bmp/login 账号 test004 密码 MyPass123 动态口令 260326 "
        "范围 正常,异常 代码目录 C:\\code\\repo"
    )
    assert parsed.login_url == "https://example.com/bmp/login"
    assert parsed.url == "https://example.com"
    assert parsed.user == "test004"
    assert parsed.password == "MyPass123"
    assert parsed.otp == "260326"
    assert parsed.scopes == ["正常", "异常"]
    assert parsed.local_path == "C:\\code\\repo"
    assert parsed.has_credentials()


def test_space_style_value_may_equal_an_alias_without_swallowing_next_field():
    """值恰好长得像标签（用户名就叫 `user`）时，空格式的值只取单个 token，不得吃穿到下一个字段。"""
    parsed = parse_auto_input("用户名 user 密码 Pass1234")
    assert parsed.user == "user"
    assert parsed.password == "Pass1234"


def test_unknown_space_label_words_are_not_swallowed():
    """空格式**只认已知别名**：`角色 admin` 里的 `角色` 不是标签，应作为未识别片段如实回报。"""
    parsed = parse_auto_input("角色 admin")
    assert parsed.user == ""
    assert "角色" in parsed.unknown or any("角色" in u for u in parsed.unknown)


# ---------------------------------------------------------------- 凭证红线
def test_redacted_view_never_exposes_plaintext_credentials():
    parsed = parse_auto_input("账号: someone@corp.com 密码: SuperSecret9 动态码: 260909")
    blob = str(parsed.redacted())
    assert "SuperSecret9" not in blob
    assert "someone@corp.com" not in blob
    assert "260909" not in blob
    assert parsed.redacted()["password"] == "***"
    assert parsed.redacted()["has_credentials"] is True


def test_mask_keeps_short_values_unreadable():
    assert mask_secret("") == ""
    assert mask_secret("ab") == "****"
    assert mask_secret("abcdefghij").startswith("ab")
    assert "cdef" not in mask_secret("abcdefghij")


def test_every_declared_label_group_has_a_setter_and_alias():
    """防漂移：`_LABEL_GROUPS` 里的每个字段都必须有写入器与别名，否则标签被识别却写不进去。"""
    assert set(_LABEL_GROUPS) == set(_SETTERS)
    for field_name, aliases in _LABEL_GROUPS.items():
        assert aliases, f"{field_name} 没有任何别名"
        for alias in aliases:
            assert _LABEL_ALIASES[_normalize_label(alias)] == field_name


# ---------------------------------------------------------------- 应用到选项
def test_auto_input_fills_options_with_mode_and_scopes():
    """输入框可覆盖选项的**默认值**（mode/scopes 有默认值，故允许覆盖）。"""
    opts = pipeline.default_options("")
    pipeline.apply_auto_input(opts, parse_auto_input("路径: /tmp/whatever 范围: 正常 模式: 增量"))
    assert opts.mode == "incremental"
    assert opts.scopes == {"正常"}
    assert opts.local_path == "/tmp/whatever"


def test_explicit_cli_args_win_over_auto_input(fresh_db, sample_repo, capsys):
    """端到端优先级：显式 `--mode` > 智能输入框 > 默认值（CLI 先填输入框、后应用显式参数）。"""
    from cli.main import main

    rc = main(
        [
            "pipeline",
            "--path",
            str(sample_repo),
            "--mode",
            "full",
            "--auto-input",
            "模式: 增量",
            "--no-output",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["mode"] == "full", "显式参数必须压过输入框"


def test_auto_input_enables_target_channel_and_carries_credentials():
    opts = pipeline.default_options("")
    pipeline.apply_auto_input(
        opts, parse_auto_input("地址: http://h:8090 账号: u@x.com 密码: Tp123456")
    )
    assert opts.target_req.enabled is True
    assert opts.target_req.base_url == "http://h:8090"
    assert opts.target_req.login_user == "u@x.com"
    assert opts.target_req.login_password == "Tp123456"
