"""M10：Prompt Injection 防护（不可信页面内容隔离）的单元测试。

覆盖三层防护里可离线验证的部分：
① prompt 层：边界声明是否真的写进去了
② 动作层：高危动作是否被硬拦，且拦截点确实在全局 execute_action 上
③ 域名层：warn / strict / off 三种模式的行为差异
"""

import asyncio

import pytest

from apps.ui_automation import ai_base


class TestExtractHosts:
    def test_extracts_hosts_from_task_text(self):
        text = "打开 http://localhost:3900/login 登录，然后访问 https://admin.example.com/a?b=1"
        assert ai_base._extract_hosts(text) == {'localhost', 'admin.example.com'}

    def test_ignores_plain_text_and_relative_paths(self):
        assert ai_base._extract_hosts("点击菜单 用户管理 后输入用户名") == set()

    def test_host_is_normalized_to_lowercase_without_port(self):
        assert ai_base._extract_hosts("see HTTP://Example.COM:8080/x") == {'example.com'}


class TestHostAllowed:
    def test_exact_match(self):
        assert ai_base._host_allowed('example.com', {'example.com'})

    def test_subdomain_match(self):
        assert ai_base._host_allowed('a.b.example.com', {'example.com'})

    def test_suffix_spoofing_is_rejected(self):
        # evil-example.com 不能因为以 example.com 结尾就放行
        assert not ai_base._host_allowed('evil-example.com', {'example.com'})

    def test_empty_host_is_allowed(self):
        assert ai_base._host_allowed('', {'example.com'})


class TestIterUrls:
    def test_walks_nested_params(self):
        params = {'tabs': [{'url': 'https://a.com/x'}], 'meta': {'back': 'http://b.com'}, 'n': 1}
        assert set(ai_base._iter_urls(params)) == {'https://a.com/x', 'http://b.com'}

    def test_ignores_non_url_strings(self):
        assert list(ai_base._iter_urls({'text': 'hello', 'code': '1+1'})) == []


class TestActionGuardDefaults:
    def test_default_blocks_high_risk_actions(self, monkeypatch):
        for name in ('UI_BLOCKED_ACTIONS', 'UI_ALLOWED_DOMAINS', 'UI_DOMAIN_GUARD'):
            monkeypatch.delenv(name, raising=False)
        guard = ai_base._ActionGuard.for_task("打开 http://localhost:3900")
        for action in ('evaluate', 'upload_file', 'write_file', 'replace_file', 'read_file'):
            assert guard.check(action, {}) is not None

    def test_normal_actions_are_not_blocked(self, monkeypatch):
        monkeypatch.delenv('UI_BLOCKED_ACTIONS', raising=False)
        guard = ai_base._ActionGuard.for_task("打开 http://localhost:3900")
        for action in ('click', 'input', 'scroll', 'navigate', 'done'):
            assert guard.check(action, {'index': 1}) is None

    def test_env_overrides_blocked_actions(self, monkeypatch):
        monkeypatch.setenv('UI_BLOCKED_ACTIONS', 'evaluate')
        guard = ai_base._ActionGuard.for_task("打开 http://localhost:3900")
        assert guard.check('evaluate', {}) is not None
        assert guard.check('upload_file', {}) is None, '显式配置应完全接管默认黑名单'

    def test_empty_env_disables_action_blocklist(self, monkeypatch):
        monkeypatch.setenv('UI_BLOCKED_ACTIONS', '')
        guard = ai_base._ActionGuard.for_task("打开 http://localhost:3900")
        assert guard.check('evaluate', {}) is None

    def test_allowed_domains_merge_task_and_env(self, monkeypatch):
        monkeypatch.setenv('UI_ALLOWED_DOMAINS', 'api.example.com, .corp.local')
        guard = ai_base._ActionGuard.for_task("打开 http://localhost:3900")
        assert 'localhost' in guard.allowed_domains
        assert 'api.example.com' in guard.allowed_domains
        assert 'corp.local' in guard.allowed_domains


class TestDomainGuard:
    def test_empty_whitelist_never_blocks_even_in_strict(self, monkeypatch):
        """任务里没写 URL、也没配 UI_ALLOWED_DOMAINS 时，strict 不能把功能拦死。"""
        monkeypatch.setenv('UI_DOMAIN_GUARD', 'strict')
        monkeypatch.delenv('UI_ALLOWED_DOMAINS', raising=False)
        guard = ai_base._ActionGuard.for_task("随便点点页面上的按钮")
        assert guard.allowed_domains == set()
        assert guard.check('navigate', {'url': 'https://anything.example/x'}) is None

    def test_warn_mode_allows_but_logs(self, monkeypatch, caplog):
        monkeypatch.setenv('UI_DOMAIN_GUARD', 'warn')
        monkeypatch.delenv('UI_ALLOWED_DOMAINS', raising=False)
        guard = ai_base._ActionGuard.for_task("打开 http://localhost:3900")
        with caplog.at_level('WARNING'):
            assert guard.check('navigate', {'url': 'https://evil.example/x'}) is None
        assert any('navigating outside allowed domains' in r.getMessage() for r in caplog.records)

    def test_strict_mode_blocks_external_domain(self, monkeypatch):
        monkeypatch.setenv('UI_DOMAIN_GUARD', 'strict')
        monkeypatch.delenv('UI_ALLOWED_DOMAINS', raising=False)
        guard = ai_base._ActionGuard.for_task("打开 http://localhost:3900")
        reason = guard.check('navigate', {'url': 'https://evil.example/x'})
        assert reason and 'blocked' in reason

    def test_strict_mode_allows_whitelisted_domain(self, monkeypatch):
        monkeypatch.setenv('UI_DOMAIN_GUARD', 'strict')
        guard = ai_base._ActionGuard.for_task("打开 http://localhost:3900")
        assert guard.check('navigate', {'url': 'http://localhost:3900/users'}) is None

    def test_strict_mode_ignores_non_url_params(self, monkeypatch):
        monkeypatch.setenv('UI_DOMAIN_GUARD', 'strict')
        guard = ai_base._ActionGuard.for_task("打开 http://localhost:3900")
        assert guard.check('input', {'index': 3, 'text': 'hello world'}) is None

    def test_off_mode_skips_domain_check(self, monkeypatch):
        monkeypatch.setenv('UI_DOMAIN_GUARD', 'off')
        guard = ai_base._ActionGuard.for_task("打开 http://localhost:3900")
        assert guard.check('navigate', {'url': 'https://evil.example/x'}) is None

    def test_unknown_mode_falls_back_to_warn(self, monkeypatch):
        monkeypatch.setenv('UI_DOMAIN_GUARD', 'nonsense')
        guard = ai_base._ActionGuard.for_task("打开 http://localhost:3900")
        assert guard.domain_mode == 'warn'


class TestPipelineHook:
    """验证守卫真的接在全局 execute_action 上（而不只是个孤立的类）。"""

    def test_blocked_action_never_reaches_original(self, monkeypatch):
        from browser_use.tools.registry.service import Registry

        reached = []

        async def fake_original(_self, action_name, params, **kwargs):
            reached.append(action_name)
            return 'executed'

        monkeypatch.setattr(ai_base, '_original_execute_action', fake_original)
        guard = ai_base._ActionGuard(blocked_actions={'evaluate'})
        token = ai_base._current_action_guard.set(guard)
        try:
            result = asyncio.run(Registry.execute_action(object(), 'evaluate', {'code': 'fetch("http://x")'}))
        finally:
            ai_base._current_action_guard.reset(token)

        assert reached == [], '被拦的动作不应到达真正的执行器'
        assert 'security-guard' in str(getattr(result, 'error', result))

    def test_allowed_action_passes_through(self, monkeypatch):
        from browser_use.tools.registry.service import Registry

        reached = []

        async def fake_original(_self, action_name, params, **kwargs):
            reached.append(action_name)
            return 'executed'

        monkeypatch.setattr(ai_base, '_original_execute_action', fake_original)
        token = ai_base._current_action_guard.set(ai_base._ActionGuard(blocked_actions={'evaluate'}))
        try:
            assert asyncio.run(Registry.execute_action(object(), 'click', {'index': 1})) == 'executed'
        finally:
            ai_base._current_action_guard.reset(token)
        assert reached == ['click']

    def test_no_guard_means_no_change(self, monkeypatch):
        from browser_use.tools.registry.service import Registry

        async def fake_original(_self, action_name, params, **kwargs):
            return 'executed'

        monkeypatch.setattr(ai_base, '_original_execute_action', fake_original)
        # 未设置 contextvar（默认 None）时，行为与打补丁前一致
        assert asyncio.run(Registry.execute_action(object(), 'evaluate', {'code': '1'})) == 'executed'


class TestWiring:
    """防回退：确认 run_task 真的把守卫和边界段接上了（否则只是死代码）。"""

    def test_run_task_wires_guard_and_prompt(self):
        import inspect

        src = inspect.getsource(ai_base.BaseBrowserAgent.run_task)
        assert '_security_boundary_prompt()' in src
        assert '_current_action_guard.set(' in src
        assert '_current_action_guard.reset(' in src


class TestSecurityBoundaryPrompt:
    def test_prompt_states_untrusted_data_and_key_rules(self):
        text = ai_base._security_boundary_prompt()
        assert 'UNTRUSTED DATA' in text
        assert 'HOSTILE TEXT' in text
        assert 'credentials' in text.lower()
        assert 'SECURITY BOUNDARY' in text

    def test_prompt_is_not_empty_whitespace_noise(self):
        assert len(ai_base._security_boundary_prompt().strip()) > 300


@pytest.mark.parametrize('action', ['evaluate', 'upload_file'])
def test_blocklist_covers_exfiltration_paths(action, monkeypatch):
    """注入后最常见的两条外发路径必须默认关闭。"""
    monkeypatch.delenv('UI_BLOCKED_ACTIONS', raising=False)
    guard = ai_base._ActionGuard.for_task("打开 http://localhost:3900")
    assert guard.check(action, {'code': 'x', 'path': 'x'}) is not None
