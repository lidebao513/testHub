"""活体验证：页面预探索（#177）在真实浏览器会话上能否产出元素清单。

不依赖 LLM：只走 `BrowserSession.get_browser_state_summary()` → `format_pre_explore_elements()`
这一条与 `BaseBrowserAgent._run_pre_explore` 完全相同的链路，验证 DOM 结构兼容性。

用法（服务已启动）：
    venv/Scripts/python.exe tests/verify_pre_explore_live.py [url]

产物：stdout 打印元素数量与前若干项；失败退出码非 0。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    # 直接跑脚本时 sys.path[0] 是 tests/，必须把仓库根加回来才能 import backend/apps
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")

import django  # noqa: E402

django.setup()

from browser_use import BrowserSession  # noqa: E402
from browser_use.browser.profile import BrowserProfile  # noqa: E402

from apps.ui_automation import ai_base  # noqa: E402

DEFAULT_URL = "http://127.0.0.1:3900/login"


async def main(url: str) -> int:
    # enable_default_extensions=False 与 ai_base._create_browser_profile 保持一致：
    # 否则 browser_use 会去下载 uBlock/ClearURLs 等扩展，本机代理下必然超时数十秒。
    profile = BrowserProfile(
        headless=True,
        enable_default_extensions=False,
        args=["--no-proxy-server", "--no-sandbox"],
    )
    session = BrowserSession(browser_profile=profile)
    await session.start()
    try:
        page = await session.get_current_page()
        if page is None:
            print("[live] FAIL: 没有可用页面")
            return 1
        await session.navigate_to(url)
        await asyncio.sleep(2.5)

        state = await session.get_browser_state_summary(include_screenshot=False)
        selector_map = getattr(state.dom_state, "selector_map", None) or {}
        elements = ai_base.format_pre_explore_elements(selector_map)
        block = ai_base.render_pre_explore_block(
            elements, url=str(getattr(state, "url", "") or ""), title=str(getattr(state, "title", "") or "")
        )

        print(f"[live] raw selector_map entries: {len(selector_map)}")
        print(block)
        if not elements:
            print("[live] FAIL: 未提取到任何可交互元素")
            return 1
        print(f"[live] OK: 提取 {len(elements)} 项")
        return 0
    finally:
        await session.kill()


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    raise SystemExit(asyncio.run(main(target)))
