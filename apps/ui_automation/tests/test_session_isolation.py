"""#180 会话隔离：独立 user_data_dir + 动态调试端口的单元测试。

背景：旧实现把 Chrome 调试端口硬编码为 9222（启动参数 + Linux watchdog patch +
connect 轮询 + 僵尸进程清理共 6 处），并发跑两个任务时会互相抢 9222、甚至把对方
的浏览器当成自己的（跨会话串浏览器）。

修复后的分工：
- 调试端口：由 browser-use 的 LocalBrowserWatchdog 每次启动动态分配（bind 127.0.0.1:0）。
- user_data_dir：BrowserProfile 的 user_data_dir 保持 None，由 browser-use 校验器
  为每个 profile 实例自动 mkdtemp 独立临时目录（显式自建目录反而触发其拷贝逻辑）。
- 僵尸清理：只杀"user-data-dir 已消失"的孤儿进程，不再按 9222 子串匹配。
"""

import asyncio
import inspect
import socket
import types

import pytest

from apps.ui_automation import ai_base


def _make_agent() -> ai_base.BaseBrowserAgent:
    # 绕过 __init__（依赖 DB 的 AIModelConfig），只测 profile 构建逻辑
    return ai_base.BaseBrowserAgent.__new__(ai_base.BaseBrowserAgent)


class TestProfileSessionIsolation:
    """每个任务会话独立的 user_data_dir，互不污染。"""

    def test_two_profiles_get_distinct_user_data_dirs(self):
        agent = _make_agent()
        p1 = agent._create_browser_profile()
        p2 = agent._create_browser_profile()
        assert p1.user_data_dir and p2.user_data_dir
        assert str(p1.user_data_dir) != str(p2.user_data_dir)

    def test_user_data_dir_is_a_temp_dir(self):
        import tempfile

        agent = _make_agent()
        p = agent._create_browser_profile()
        tmp_root = tempfile.gettempdir().lower()
        assert tmp_root in str(p.user_data_dir).lower()

    def test_profile_args_have_no_hardcoded_debug_port(self):
        agent = _make_agent()
        p = agent._create_browser_profile()
        assert not any("remote-debugging-port" in str(a) for a in p.args), (
            "启动参数不得再固定调试端口，应由 watchdog 动态分配"
        )

    def test_concurrent_profiles_isolated(self):
        """并发构建两个会话 profile：目录独立、端口不冲突。"""
        agent = _make_agent()

        async def build():
            await asyncio.sleep(0)
            return agent._create_browser_profile()

        async def main():
            return await asyncio.gather(build(), build())

        p1, p2 = asyncio.run(main())
        assert str(p1.user_data_dir) != str(p2.user_data_dir)
        assert not any("remote-debugging-port" in str(a) for a in p1.args + p2.args)


class TestDynamicDebugPort:
    """端口由 watchdog 动态分配，且旧的强制 9222 patch 已移除。"""

    def test_find_free_port_not_patched_to_constant(self):
        from browser_use.browser.watchdogs.local_browser_watchdog import (
            LocalBrowserWatchdog,
        )

        src = inspect.getsource(LocalBrowserWatchdog._find_free_port)
        assert "9222" not in src, "watchdog 端口分配不得再出现 9222 常量"

    def test_two_allocated_ports_are_bindable_and_distinct(self):
        from browser_use.browser.watchdogs.local_browser_watchdog import (
            LocalBrowserWatchdog,
        )

        port1 = LocalBrowserWatchdog._find_free_port()
        # 模拟第一个浏览器已监听该端口
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            holder.bind(("127.0.0.1", port1))
            holder.listen(1)
            port2 = LocalBrowserWatchdog._find_free_port()
            # 第二次分配不能拿到已被占用的端口（并发不抢端口的可测代理）
            assert port2 != port1
            s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s2.bind(("127.0.0.1", port2))
            finally:
                s2.close()
        finally:
            holder.close()


class TestOrphanChromeDetection:
    """僵尸清理只杀 user-data-dir 已消失的孤儿，不再按 9222 匹配。"""

    def test_orphan_when_user_data_dir_missing(self, tmp_path):
        gone_dir = tmp_path / "browser-use-user-data-dir-gone"
        cmdline = [
            "/usr/bin/chromium",
            f"--user-data-dir={gone_dir}",
            "--remote-debugging-port=41234",
        ]
        assert ai_base.BaseBrowserAgent._is_orphaned_chrome_cmdline(cmdline) is True

    def test_not_orphan_when_user_data_dir_alive(self, tmp_path):
        live_dir = tmp_path / "browser-use-user-data-dir-live"
        live_dir.mkdir()
        cmdline = [
            "/usr/bin/chromium",
            f"--user-data-dir={live_dir}",
            "--remote-debugging-port=41235",
        ]
        assert ai_base.BaseBrowserAgent._is_orphaned_chrome_cmdline(cmdline) is False

    def test_not_orphan_without_user_data_dir_flag(self):
        cmdline = ["/usr/bin/chromium", "--remote-debugging-port=9222"]
        # 没有可判定依据时 fail-closed：宁可不杀
        assert ai_base.BaseBrowserAgent._is_orphaned_chrome_cmdline(cmdline) is False

    def test_empty_cmdline_is_not_orphan(self):
        assert ai_base.BaseBrowserAgent._is_orphaned_chrome_cmdline([]) is False
        assert ai_base.BaseBrowserAgent._is_orphaned_chrome_cmdline(None) is False

    def test_cleanup_no_longer_matches_port_substring(self):
        src = inspect.getsource(ai_base.BaseBrowserAgent._cleanup_zombie_chrome)
        assert "9222" not in src, "清理逻辑不得再按 9222 端口匹配（会误杀并发会话）"


class TestConnectPatch:
    """BrowserSession.connect patch：不再按固定端口轮询。"""

    def test_patched_connect_source_has_no_fixed_port_polling(self):
        from browser_use.browser.session import BrowserSession

        src = inspect.getsource(BrowserSession.connect)
        assert "9222" not in src
        assert "localhost:" not in src, "connect 不得再自行轮询固定端口"

    def test_cdp_url_arg_passes_through(self):
        """显式传入 cdp_url 时直接透传（第一行短路）。"""
        fake_self = types.SimpleNamespace(
            browser_profile=types.SimpleNamespace(cdp_url=None),
            cdp_url=None,
        )
        # 真实 connect 第一行会把 cdp_url 写进 profile，随后因 self.cdp_url 为空抛
        # RuntimeError——正好可断言透传值，且无网络调用。
        with pytest.raises(RuntimeError):
            asyncio.run(
                ai_base.BrowserSession.connect(
                    fake_self, cdp_url="http://127.0.0.1:19999/json/version"
                )
            )
        assert (
            fake_self.browser_profile.cdp_url == "http://127.0.0.1:19999/json/version"
        )

    def test_bounded_retry_when_cdp_not_ready(self, monkeypatch):
        """CDP 未就绪时走有界重试（3 次），最终把异常抛出，不无限轮询。"""
        sleeps: list[float] = []

        async def _fake_sleep(delay):
            sleeps.append(delay)

        monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

        fake_self = types.SimpleNamespace(
            browser_profile=types.SimpleNamespace(cdp_url=None),
            cdp_url=None,
        )
        # profile 与参数均无 cdp_url → 真实 connect 抛 RuntimeError → 重试后仍抛出
        with pytest.raises(RuntimeError):
            asyncio.run(ai_base.BrowserSession.connect(fake_self, cdp_url=None))
        # 3 次尝试之间有 2 次重试等待
        assert sleeps == [1.0, 1.0]
