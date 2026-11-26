"""PW-3b 3b-1：只读 CDP attach 调试后门。

边界：

- 默认关闭：必须显式配置 ``UI_CDP_URL``，否则入口直接拒绝。
- 只读：只连接现有浏览器，读取当前页标题/URL/元素数；不导航、不点击、不输入。
- 不接执行链：本模块不创建 AI 执行器，不调用任务执行入口，不承担多任务并发。
- 凭据卫生：CDP URL 可能带 token，日志只记 host/port，错误只记异常类型。

用法：

    UI_CDP_URL=http://127.0.0.1:9222 python -m apps.ui_automation.debug_attach
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("django")

_ALLOWED_SCHEMES = {"http", "https", "ws", "wss"}


class DebugAttachDisabled(RuntimeError):
    """未显式配置 UI_CDP_URL，调试后门默认关闭。"""


class DebugAttachConfigError(ValueError):
    """UI_CDP_URL 格式不合法。"""


@dataclass(frozen=True)
class AttachProbeResult:
    """只读探测结果。"""

    title: str
    url: str
    element_count: int


def configured_cdp_url() -> str:
    """读取 ``UI_CDP_URL``；缺省空串，必须显式指定。"""
    return (os.getenv("UI_CDP_URL") or "").strip()


def safe_cdp_label(url: str) -> str:
    """只保留 host[:port]，供日志使用；绝不输出 query/userinfo/path。"""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return "<invalid-cdp-url>"
    if not host:
        return "<invalid-cdp-url>"
    return f"{host}:{port}" if port else host


def resolve_cdp_url(cdp_url: str | None = None) -> str:
    """解析并校验 CDP URL；未配置时默认关闭，格式错误统一脱敏报错。"""
    raw = configured_cdp_url() if cdp_url is None else str(cdp_url).strip()
    if not raw:
        raise DebugAttachDisabled("未配置 UI_CDP_URL，只读 attach 后门默认关闭")
    try:
        parsed = urlparse(raw)
        host = parsed.hostname
        _ = parsed.port  # 触发端口合法性校验，避免非法端口流入 browser-use
    except ValueError as exc:
        raise DebugAttachConfigError("UI_CDP_URL 格式不合法") from exc
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES or not host:
        raise DebugAttachConfigError("UI_CDP_URL 格式不合法")
    return raw


def build_attach_profile(
    cdp_url: str | None = None,
    profile_factory: Callable[..., Any] | None = None,
) -> Any:
    """构造 attach 专用 BrowserProfile 参数，不启动新浏览器、不复用执行链 profile。"""
    url = resolve_cdp_url(cdp_url)
    if profile_factory is None:
        from browser_use.browser.profile import BrowserProfile

        profile_factory = BrowserProfile
    logger.info("只读 attach profile 构造完成 target=%s", safe_cdp_label(url))
    return profile_factory(
        cdp_url=url,
        user_data_dir=None,
        is_local=False,
        executable_path=None,
    )


async def probe_attached_page(
    cdp_url: str | None = None,
    *,
    session_factory: Callable[..., Any] | None = None,
    profile_factory: Callable[..., Any] | None = None,
) -> AttachProbeResult:
    """只读探测当前 attach 页面；退出时 detach，不关闭外部浏览器。"""
    url = resolve_cdp_url(cdp_url)
    profile = build_attach_profile(url, profile_factory=profile_factory)

    if session_factory is None:
        from browser_use.browser.session import BrowserSession

        session_factory = BrowserSession

    session = None
    try:
        session = session_factory(browser_profile=profile)
        await session.connect(cdp_url=url)
        # 只读探测核心是 title/URL：证明 attach 通路成立。元素数只是附加信息，
        # browser-use 0.10.1 的 get_browser_state_summary 依赖 Agent 注册的事件
        # handler，裸 BrowserSession.connect() 下会抛 ValueError，不能让它阻断探测。
        title = await session.get_current_page_title()
        page_url = await session.get_current_page_url()
        element_count = 0
        try:
            summary = await session.get_browser_state_summary(
                include_screenshot=False,
                cached=False,
            )
            dom_state = getattr(summary, "dom_state", None)
            selector_map = getattr(dom_state, "selector_map", None) or {}
            element_count = len(selector_map)
        except TypeError:
            pass
        except Exception as exc:  # noqa: BLE001 - 元素数降级，不掩盖title/URL结果
            logger.warning(
                "只读 attach DOM 摘要不可用，元素数降级为 0 target=%s error=%s",
                safe_cdp_label(url),
                type(exc).__name__,
            )
        result = AttachProbeResult(
            title=str(title),
            url=str(page_url),
            element_count=element_count,
        )
        logger.info(
            "只读 attach 探测完成 target=%s elements=%d",
            safe_cdp_label(url),
            element_count,
        )
        return result
    except Exception as exc:
        logger.warning(
            "只读 attach 探测失败 target=%s error=%s",
            safe_cdp_label(url),
            type(exc).__name__,
        )
        raise
    finally:
        if session is not None:
            try:
                await session.stop()
            except Exception as exc:  # noqa: BLE001 - detach 清理失败不能掩盖探测结果
                logger.warning(
                    "只读 attach detach 失败 target=%s error=%s",
                    safe_cdp_label(url),
                    type(exc).__name__,
                )


@contextmanager
def _logs_to_stderr():
    """--json 模式把 root logger 的 stdout handler 临时改道 stderr。

    browser-use import 时会给 root logger 挂一个指向 stdout 的 StreamHandler，
    django logger 的探测日志 propagate 上去会污染 --json 输出。这里只在本模块
    调用期间改道并恢复，不改变全局日志配置，也不影响非 --json 模式。
    """
    switched: list[tuple[logging.Handler, Any]] = []
    for handler in logging.getLogger().handlers:
        stream = getattr(handler, "stream", None)
        if isinstance(handler, logging.StreamHandler) and stream is sys.stdout:
            handler.setStream(sys.stderr)
            switched.append((handler, stream))
    try:
        yield
    finally:
        for handler, original in switched:
            handler.setStream(original)


def main(argv: list[str] | None = None) -> int:
    """命令行入口：只读打印当前页标题/URL/元素数。"""
    parser = argparse.ArgumentParser(description="只读 attach 探测当前浏览器页面")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出探测结果")
    args = parser.parse_args(argv)

    try:
        if args.json:
            # browser-use 在 import 时才把 StreamHandler 挂到 root/stdout，
            # 必须先 import 触发挂载，_logs_to_stderr 才能捕获到它并改道。
            from browser_use.browser.session import BrowserSession  # noqa: F401

            with _logs_to_stderr():
                result = asyncio.run(probe_attached_page())
        else:
            result = asyncio.run(probe_attached_page())
    except (DebugAttachDisabled, DebugAttachConfigError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI 顶层兜底，只输出异常类型不泄露 CDP URL
        print(f"只读 attach 探测失败: {type(exc).__name__}", file=sys.stderr)
        return 1

    payload = {
        "title": result.title,
        "url": result.url,
        "element_count": result.element_count,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"title: {payload['title']}")
        print(f"url: {payload['url']}")
        print(f"element_count: {payload['element_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
