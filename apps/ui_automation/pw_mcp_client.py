"""Playwright MCP client —— #185 阶段一 spike 的浏览器控制通道。

职责：
- 用 Python mcp SDK 经 stdio 拉起 ``@playwright/mcp`` 子进程（node cli.js）。
- 封装 connect / list_tools / call_tool / close。
- 收集每次工具响应的 codegen 片段（Playwright 代码片段），组装为可运行脚本。

安全（与 apps/ui_automation/ai_base.py 的 _ActionGuard 安全语义保持一致）：
- 目标 URL 必须过最小硬拦截（云元数据 169.254.0.0/16 + 内网私有段），
  复用 ai_base 的纯函数算法（此处内联手写，便于无 Django 依赖的探针/单测复用；
  **阶段二**会抽取为 ui_automation 内共享 guard 模块，本文件改 import 复用）。
- 启动命令**严禁**出现 ``browser_run_code_unsafe``（RCE 等价）。
- 凭据不进脚本/日志/响应体（由调用方在组装层负责，见 assemble_script）。
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
from contextlib import AsyncExitStack
from typing import Any
from urllib.parse import urlparse

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

logger = logging.getLogger(__name__)


# ============================================================================
# 最小安全硬拦截（与 ai_base._ActionGuard 安全语义一致，纯函数，无 Django 依赖）
# ============================================================================
_METADATA_NETS = (ipaddress.ip_network("169.254.0.0/16"),)
_METADATA_IPS = frozenset({ipaddress.ip_address("100.100.100.200")})
_METADATA_HOSTNAMES = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "metadata.azure.com",
        "metadata.tencentyun.com",
        "metadata.tencent.internal",
        "instance-data",
        "instance-data.ec2.internal",
    }
)
_PRIVATE_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("fc00::/7"),
)
_LOOPBACK_NETS = (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))
_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})


def _normalize_host(host: str) -> str:
    return str(host or "").strip().lower().strip("[]") or ""


def _parse_ip_literal(host):
    text = _normalize_host(host)
    if not text:
        return None
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        pass
    try:
        if text[:2].lower() == "0x":
            number = int(text, 16)
        elif text.isdigit():
            number = int(text, 10)
        else:
            return None
    except ValueError:
        return None
    if 0 <= number <= 0xFFFFFFFF:
        return ipaddress.IPv4Address(number)
    return None


def is_safe_target_url(url: str) -> bool:
    """spike 最小校验：云元数据/内网私有段硬拦，loopback 与公网放行。

    注：权威实现见 apps/ui_automation/ai_base.py 的 _ActionGuard，阶段二统一抽取。
    """
    if not url:
        return False
    host = _normalize_host(urlparse(url).hostname)
    if not host:
        return False
    if host in _METADATA_HOSTNAMES:
        return False
    ip = _parse_ip_literal(host)
    if ip is not None:
        if ip in _METADATA_IPS:
            return False
        if any(ip.version == n.version and ip in n for n in _METADATA_NETS):
            return False
        if host in _LOOPBACK_NAMES or any(
            ip.version == n.version and ip in n for n in _LOOPBACK_NETS
        ):
            return True
        if any(ip.version == n.version and ip in n for n in _PRIVATE_NETS):
            return False
    return True


# ============================================================================
# codegen 提取
# ============================================================================
# Playwright MCP 的工具响应会在末尾附加 Playwright 代码片段，格式为
# fenced code block（```python ... ```）或 ``// Generated:`` 单行片段。
_FENCED_RE = re.compile(r"```(?:python|py|js|javascript)?\s*\n(.*?)```", re.DOTALL)
_GENERATED_RE = re.compile(r"//\s*Generated:\s*(.+)")


def extract_codegen(result: Any) -> list[str]:
    """从一次 MCP 工具响应中提取 Playwright 代码片段。"""
    snippets: list[str] = []
    contents = getattr(result, "content", None) or []
    for block in contents:
        text = getattr(block, "text", "")
        if not text:
            continue
        for m in _FENCED_RE.finditer(text):
            code = m.group(1).strip()
            if code:
                snippets.append(code)
        for line in text.splitlines():
            g = _GENERATED_RE.search(line)
            if g:
                snippets.append(g.group(1).strip())
    return snippets


def _first_input_ref(snapshot_result: Any) -> str | None:
    """从 snapshot 响应文本里挑出第一个可输入元素的 ref（如 e12）。

    仅用于 spike 的确定性演示，避免引入 LLM 决策；返回 None 表示无可输入元素。
    """
    contents = getattr(snapshot_result, "content", None) or []
    text = "\n".join(getattr(b, "text", "") or "" for b in contents)
    # accessibility snapshot 形如：``- textbox "New todo" [ref=e12]``
    for m in re.finditer(r"\[ref=([^\]]+)\]", text):
        ref = m.group(1)
        # 优先挑 textbox / input 附近的 ref
        start = max(0, m.start() - 60)
        ctx = text[start : m.start()]
        if "textbox" in ctx.lower() or "input" in ctx.lower():
            return ref
    return None


# ============================================================================
# 客户端
# ============================================================================
class PlaywrightMcpClient:
    """经 stdio 拉起 @playwright/mcp 的 MCP client。"""

    def __init__(
        self,
        browser: str = "chromium",
        codegen_lang: str = "python",
        node_exe: str | None = None,
        cli_js: str | None = None,
    ) -> None:
        self.node_exe = node_exe or os.getenv("NODE_EXE") or "node"
        self.cli_js = cli_js or os.getenv("PLAYWRIGHT_MCP_CLI") or _default_cli_js()
        self.browser = browser
        self.codegen_lang = codegen_lang
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    def _build_params(self) -> StdioServerParameters:
        # 严禁 browser_run_code_unsafe（RCE 等价）
        args = [self.cli_js, "--codegen", self.codegen_lang, "--browser", self.browser]
        return StdioServerParameters(
            command=self.node_exe, args=args, env=dict(os.environ)
        )

    async def connect(self) -> PlaywrightMcpClient:
        self._stack = AsyncExitStack()
        read, write = await self._stack.enter_async_context(
            stdio_client(self._build_params())
        )
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._session = session
        logger.info(
            "🔌 PlaywrightMcpClient connected (browser=%s, codegen=%s)",
            self.browser,
            self.codegen_lang,
        )
        return self

    async def list_tools(self):
        if self._session is None:
            raise RuntimeError("not connected")
        return await self._session.list_tools()

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None):
        if self._session is None:
            raise RuntimeError("not connected")
        logger.debug("call_tool %s %s", name, arguments)
        return await self._session.call_tool(name, arguments or {})

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
            self._session = None

    # ---- 高层：录制一个最小流程并产出脚本（spike 验证用）----
    async def record_flow(self, url: str, demo_text: str = "spike demo task") -> str:
        """对给定 URL 跑一个确定性最小流程（navigate -> snapshot -> 找到输入框则输入并回车），
        收集每一步的 codegen，组装为可运行 Playwright 脚本。

        这是 spike 验证路径，不含 LLM 决策（LLM 决策 loop 在阶段二）。
        """
        if not is_safe_target_url(url):
            raise ValueError(f"unsafe target url blocked by security guard: {url}")

        snippets: list[str] = []
        r = await self.call_tool("browser_navigate", {"url": url})
        snippets += extract_codegen(r)

        r = await self.call_tool("browser_snapshot", {})
        snippets += extract_codegen(r)

        ref = _first_input_ref(r)
        if ref:
            # 注意：browser_type 的入参字段是 `target`（非 `ref`），否则报
            # "Invalid arguments ... at target"。ref 来自 snapshot 的 [ref=eN]。
            r = await self.call_tool("browser_type", {"target": ref, "text": demo_text})
            snippets += extract_codegen(r)
            r = await self.call_tool("browser_press_key", {"key": "Enter"})
            snippets += extract_codegen(r)

        return assemble_script(url, snippets, language=self.codegen_lang)


def assemble_script(url: str, snippets: list[str], language: str = "python") -> str:
    """把收集到的 codegen 片段拼成一份完整可运行的 Playwright 脚本。"""
    if language == "python":
        header = (
            "from playwright.sync_api import sync_playwright\n\n"
            "def run():\n"
            "    with sync_playwright() as p:\n"
            "        browser = p.chromium.launch(headless=True)\n"
            "        page = browser.new_page()\n"
        )
        body_lines = []
        for s in snippets:
            for line in s.splitlines():
                line = line.strip()
                if not line:
                    continue
                # Playwright MCP 的 python codegen 风格不统一（navigate 为 sync、
                # keyboard.press 为 async；注释用 JS 的 // 、语句带 ;），统一转成
                # Python 可执行的 sync 风格。
                line = line.removeprefix("await ")
                if line.startswith("//"):
                    line = "#" + line[2:]
                line = line.removesuffix(";")
                body_lines.append("        " + line)
        footer = "        browser.close()\n\nif __name__ == '__main__':\n    run()\n"
        return header + ("\n".join(body_lines) + "\n" if body_lines else "") + footer
    # 暂时只实现 python；阶段二补 js
    raise ValueError(f"unsupported language: {language}")


def _default_cli_js() -> str:
    """探测 @playwright/mcp 的 cli.js 入口；找不到时返回约定路径（由环境变量兜底）。"""
    candidate = (
        "C:/Users/EDY/.workbuddy/binaries/node/versions/22.22.2-6"
        "/node_modules/@playwright/mcp/cli.js"
    )
    if os.path.exists(candidate):
        return candidate
    # 退回让上层用 PLAYWRIGHT_MCP_CLI 显式指定
    return candidate


if __name__ == "__main__":
    # 最小连通自检：仅 list_tools，不依赖浏览器内核
    async def _self_check():
        c = PlaywrightMcpClient()
        try:
            await c.connect()
            tools = await c.list_tools()
            print("CONNECT_OK tool_count=", len(tools.tools))
            for t in tools.tools:
                print("  -", t.name)
        finally:
            await c.close()

    asyncio.run(_self_check())
