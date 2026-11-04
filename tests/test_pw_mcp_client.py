"""#185 阶段一 spike 的纯函数单测（不依赖 node/chromium，跑得快）。

锁定：
- 安全硬拦截 is_safe_target_url（云元数据/内网私有段硬拦，loopback/公网放行）
- 启动参数绝不带 browser_run_code_unsafe（RCE 等价）
- assemble_script 产出合法、sync 风格的 Python（无 JS 残留）
- codegen 提取 / input ref 解析
"""
import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "ui_automation"))
import pw_mcp_client  # noqa: E402


class _Block:
    def __init__(self, text):
        self.text = text


class _Result:
    def __init__(self, text):
        self.content = [_Block(text)]


def test_is_safe_target_url_blocks_cloud_metadata():
    for u in (
        "http://169.254.169.254/",
        "http://100.100.100.200/",
        "http://metadata.google.internal/",
        "http://metadata.tencentyun.com/",
    ):
        assert pw_mcp_client.is_safe_target_url(u) is False


def test_is_safe_target_url_blocks_private():
    for u in ("http://192.168.1.1/", "http://10.0.0.5/", "http://172.16.0.1/"):
        assert pw_mcp_client.is_safe_target_url(u) is False


def test_is_safe_target_url_allows_public_and_loopback():
    assert pw_mcp_client.is_safe_target_url("https://demo.playwright.dev/todomvc") is True
    assert pw_mcp_client.is_safe_target_url("http://localhost:8900/") is True
    assert pw_mcp_client.is_safe_target_url("http://127.0.0.1:8900/") is True


def test_assemble_script_sync_and_valid_python():
    snippets = [
        'page.goto("https://demo.playwright.dev/todomvc")',
        "// Press Enter\nawait page.keyboard.press('Enter');",
        'page.get_by_role("textbox", name="x").fill("y")',
    ]
    script = pw_mcp_client.assemble_script("https://demo.playwright.dev/todomvc", snippets)
    # 无 JS 风格残留
    assert not any(line.strip().startswith("//") for line in script.splitlines())
    assert "await " not in script
    assert "sync_playwright" in script
    # 语法可编译
    ast.parse(script)


def test_extract_codegen_fenced_and_generated():
    r = _Result("### Ran Playwright code\n```python\npage.goto('x')\n```\n")
    assert pw_mcp_client.extract_codegen(r) == ["page.goto('x')"]
    r2 = _Result("did something\n// Generated: page.click('#a')\n")
    assert pw_mcp_client.extract_codegen(r2) == ["page.click('#a')"]


def test_first_input_ref():
    r = _Result('- textbox "What needs to be done?" [ref=e8]\n- link [ref=e3]')
    assert pw_mcp_client._first_input_ref(r) == "e8"
    r2 = _Result("- link [ref=e3]\n- button [ref=e5]")
    assert pw_mcp_client._first_input_ref(r2) is None


def test_build_params_never_uses_unsafe():
    c = pw_mcp_client.PlaywrightMcpClient()
    params = c._build_params()
    assert "browser_run_code_unsafe" not in " ".join(params.args)
