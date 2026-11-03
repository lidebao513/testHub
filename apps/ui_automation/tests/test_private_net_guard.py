"""#179 内网 / 云元数据拦截的单元测试。

为什么单独一个文件：这批规则是 SSRF 防线，行为差异只在"边界值"上
（169.254.169.254 vs 8.8.8.8、172.16 vs 172.32、十进制混淆 vs 点分十进制），
集中放一处才好审。层级关系见 ai_base 里 `_ActionGuard.check` 的判定顺序注释。
"""

import asyncio

import pytest

from apps.ui_automation import ai_base


class TestParseIpLiteral:
    @pytest.mark.parametrize(
        'host',
        ['169.254.169.254', '10.0.0.1', '::1', '127.0.0.1', '255.255.255.255'],
    )
    def test_standard_forms(self, host):
        assert ai_base._parse_ip_literal(host) is not None

    def test_decimal_integer_form(self):
        # http://2852039166/ 在浏览器里等于 169.254.169.254
        assert str(ai_base._parse_ip_literal('2852039166')) == '169.254.169.254'

    def test_hex_form(self):
        assert str(ai_base._parse_ip_literal('0xA9FEA9FE')) == '169.254.169.254'

    def test_bracketed_ipv6_is_unwrapped(self):
        assert str(ai_base._parse_ip_literal('[::1]')) == '::1'

    @pytest.mark.parametrize('host', ['example.com', '', None, '10.0.0.999', '999999999999'])
    def test_non_ip_returns_none(self, host):
        assert ai_base._parse_ip_literal(host) is None


class TestCloudMetadataDetection:
    @pytest.mark.parametrize(
        'host',
        [
            '169.254.169.254',  # AWS / GCP / Azure / 腾讯云通用
            '169.254.1.1',  # 整个链路本地段都算
            '100.100.100.200',  # 阿里云 ECS 元数据（不在链路本地段，容易漏）
            '2852039166',  # 十进制混淆
            '0xA9FEA9FE',  # 十六进制混淆
            'metadata.google.internal',
            'metadata.tencentyun.com',
        ],
    )
    def test_metadata_endpoints_detected(self, host):
        assert ai_base._is_cloud_metadata_host(host) is True

    @pytest.mark.parametrize('host', ['8.8.8.8', 'example.com', '192.168.1.1', '127.0.0.1'])
    def test_normal_hosts_not_metadata(self, host):
        assert ai_base._is_cloud_metadata_host(host) is False


class TestPrivateAndLoopbackDetection:
    @pytest.mark.parametrize(
        'host',
        ['10.0.0.1', '10.255.255.254', '172.16.0.1', '172.31.255.254', '192.168.1.10', 'fc00::1', '0.0.0.0'],
    )
    def test_private_ranges(self, host):
        assert ai_base._is_private_host(host) is True

    @pytest.mark.parametrize(
        'host',
        [
            '172.32.0.1',  # 172.16/12 的上界外，最容易写错的边界
            '172.15.255.255',
            '8.8.8.8',
            'example.com',
            '127.0.0.1',  # 回环单独归类
            '169.254.169.254',  # 元数据单独归类，优先级更高
        ],
    )
    def test_outside_private_ranges(self, host):
        assert ai_base._is_private_host(host) is False

    @pytest.mark.parametrize('host', ['127.0.0.1', '127.5.5.5', 'localhost', 'LOCALHOST', '::1'])
    def test_loopback(self, host):
        assert ai_base._is_loopback_host(host) is True

    def test_public_host_not_loopback(self):
        assert ai_base._is_loopback_host('example.com') is False


class TestMetadataIsHardBlocked:
    """元数据端点必须无路可绕：这是 #179 的核心断言。"""

    def test_blocked_even_when_domain_guard_off(self, monkeypatch):
        monkeypatch.setenv('UI_DOMAIN_GUARD', 'off')
        monkeypatch.setenv('UI_PRIVATE_NET_GUARD', 'off')
        guard = ai_base._ActionGuard.for_task('随便点点')
        reason = guard.check('navigate', {'url': 'http://169.254.169.254/latest/meta-data/'})
        assert reason and 'metadata' in reason

    def test_blocked_when_whitelist_is_empty(self, monkeypatch):
        """空白名单逃生口只覆盖域名层，不能覆盖元数据。"""
        monkeypatch.delenv('UI_ALLOWED_DOMAINS', raising=False)
        monkeypatch.setenv('UI_DOMAIN_GUARD', 'strict')
        guard = ai_base._ActionGuard.for_task('随便点点')
        assert guard.allowed_domains == set()
        assert guard.check('navigate', {'url': 'http://169.254.169.254/'}) is not None

    def test_blocked_even_if_explicitly_whitelisted(self, monkeypatch):
        monkeypatch.setenv('UI_ALLOWED_DOMAINS', '169.254.169.254,100.100.100.200')
        guard = ai_base._ActionGuard.for_task('随便点点')
        assert guard.check('navigate', {'url': 'http://169.254.169.254/'}) is not None
        assert guard.check('navigate', {'url': 'http://100.100.100.200/'}) is not None

    def test_blocked_in_nested_params(self, monkeypatch):
        guard = ai_base._ActionGuard.for_task('随便点点')
        params = {'steps': [{'go': 'http://169.254.169.254/x'}], 'meta': {'u': 'http://metadata.google.internal/'}}
        assert guard.check('navigate', params) is not None

    def test_block_message_says_not_configurable(self, monkeypatch):
        guard = ai_base._ActionGuard.for_task('随便点点')
        reason = guard.check('navigate', {'url': 'http://169.254.169.254/'})
        assert 'not configurable' in reason


class TestPrivateNetworkGuard:
    def test_private_blocked_by_default(self, monkeypatch):
        monkeypatch.delenv('UI_PRIVATE_NET_GUARD', raising=False)
        monkeypatch.delenv('UI_ALLOWED_DOMAINS', raising=False)
        guard = ai_base._ActionGuard.for_task('打开 http://192.168.1.50:8099')
        # 任务里显式写了该 host → 它进了白名单 → 属于"显式放行"，应通过
        assert guard.check('navigate', {'url': 'http://192.168.1.50:8099/home'}) is None

    def test_private_blocked_when_not_in_task_text(self, monkeypatch):
        monkeypatch.delenv('UI_PRIVATE_NET_GUARD', raising=False)
        monkeypatch.delenv('UI_ALLOWED_DOMAINS', raising=False)
        guard = ai_base._ActionGuard.for_task('打开 http://example.com')
        reason = guard.check('navigate', {'url': 'http://192.168.1.50:8099/home'})
        assert reason and 'private network' in reason

    def test_private_allowed_via_env_whitelist(self, monkeypatch):
        monkeypatch.delenv('UI_PRIVATE_NET_GUARD', raising=False)
        monkeypatch.setenv('UI_ALLOWED_DOMAINS', '192.168.1.50')
        guard = ai_base._ActionGuard.for_task('随便点点')
        assert guard.check('navigate', {'url': 'http://192.168.1.50:8099/home'}) is None

    def test_private_blocked_even_in_domain_off(self, monkeypatch):
        """domain_mode=off 只关域名层，内网段是独立一档。"""
        monkeypatch.setenv('UI_DOMAIN_GUARD', 'off')
        monkeypatch.delenv('UI_PRIVATE_NET_GUARD', raising=False)
        monkeypatch.delenv('UI_ALLOWED_DOMAINS', raising=False)
        guard = ai_base._ActionGuard.for_task('随便点点')
        assert guard.check('navigate', {'url': 'http://10.1.2.3/'}) is not None

    def test_warn_mode_allows_but_logs(self, monkeypatch, caplog):
        monkeypatch.setenv('UI_PRIVATE_NET_GUARD', 'warn')
        monkeypatch.delenv('UI_ALLOWED_DOMAINS', raising=False)
        guard = ai_base._ActionGuard.for_task('随便点点')
        with caplog.at_level('WARNING'):
            assert guard.check('navigate', {'url': 'http://10.1.2.3/'}) is None
        assert any('private network host' in r.getMessage() for r in caplog.records)

    def test_off_mode_skips_private_check(self, monkeypatch):
        monkeypatch.setenv('UI_PRIVATE_NET_GUARD', 'off')
        monkeypatch.delenv('UI_ALLOWED_DOMAINS', raising=False)
        guard = ai_base._ActionGuard.for_task('随便点点')
        assert guard.check('navigate', {'url': 'http://10.1.2.3/'}) is None

    def test_unknown_value_falls_back_to_block(self, monkeypatch):
        monkeypatch.setenv('UI_PRIVATE_NET_GUARD', 'nonsense')
        guard = ai_base._ActionGuard.for_task('随便点点')
        assert guard.private_mode == 'block'

    def test_loopback_allowed_by_default(self, monkeypatch):
        monkeypatch.delenv('UI_PRIVATE_NET_GUARD', raising=False)
        monkeypatch.delenv('UI_ALLOWED_DOMAINS', raising=False)
        guard = ai_base._ActionGuard.for_task('随便点点')
        assert guard.check('navigate', {'url': 'http://127.0.0.1:8900/login'}) is None
        assert guard.check('navigate', {'url': 'http://localhost:3900/'}) is None

    def test_non_url_params_untouched(self, monkeypatch):
        guard = ai_base._ActionGuard.for_task('随便点点')
        assert guard.check('input', {'index': 3, 'text': '10.0.0.1 是内网段'}) is None


class TestGuardWiring:
    def test_metadata_navigation_never_reaches_executor(self, monkeypatch):
        """接线验证：拦截真的发生在全局 execute_action 上。"""
        from browser_use.tools.registry.service import Registry

        reached = []

        async def fake_original(_self, action_name, params, **kwargs):
            reached.append(action_name)
            return 'executed'

        monkeypatch.setattr(ai_base, '_original_execute_action', fake_original)
        monkeypatch.setenv('UI_DOMAIN_GUARD', 'off')
        guard = ai_base._ActionGuard.for_task('随便点点')
        token = ai_base._current_action_guard.set(guard)
        try:
            result = asyncio.run(Registry.execute_action(object(), 'navigate', {'url': 'http://169.254.169.254/'}))
        finally:
            ai_base._current_action_guard.reset(token)

        assert reached == []
        assert 'security-guard' in str(getattr(result, 'error', result))
