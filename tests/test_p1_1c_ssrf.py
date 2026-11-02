"""1c 子批安全整改自测（H4 SSRF / M16 webhook SSRF + 日志脱敏）。

覆盖：
- ssrf_guard 纯函数：协议白名单、云元数据/链路本地/组播/保留段恒拦、
  私网与回环按 strict 策略开关、allow_hosts/CIDR 白名单、IPv4-mapped IPv6
  归一化、不可解析域名拒绝；
- safe_request：重定向逐跳重新校验（公网→302→元数据 必须拦下）、
  303/301 降级 GET 并丢 body、跳数上限；
- 日志脱敏：redact_url / mask_webhook_url；
- 接入点：api_testing 的出站调用已全部改走 safe_request（无直连 requests）、
  序列化器写入口静态预检。

设计为 DB-free：不做任何数据库访问，纯函数 + mock。
"""
import os

import django
from django.conf import settings

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
if not settings.configured:
    django.setup()

import pytest  # noqa: E402

from apps.api_testing import ssrf_guard as g  # noqa: E402
from apps.api_testing.ssrf_guard import SsrfBlocked, SsrfPolicy  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每个用例都从默认策略出发，避免宿主环境变量串味。"""
    for name in (
        'API_TESTING_SSRF_STRICT',
        'API_TESTING_SSRF_ALLOW_HOSTS',
        'API_TESTING_SSRF_MAX_REDIRECTS',
    ):
        monkeypatch.delenv(name, raising=False)
    g.reset_policy_cache()
    yield
    g.reset_policy_cache()


PERMISSIVE = SsrfPolicy(strict=False)
STRICT = SsrfPolicy(strict=True)


# --------------------------------------------------------------------------- #
# H4-a 协议白名单
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    'url',
    [
        'file:///etc/passwd',
        'gopher://127.0.0.1:6379/_INFO',
        'dict://127.0.0.1:11211/stat',
        'ftp://example.com/x',
        '//169.254.169.254/latest/meta-data/',
        'javascript:alert(1)',
        '',
    ],
)
def test_scheme_and_empty_blocked(url):
    with pytest.raises(SsrfBlocked):
        g.validate_url(url, PERMISSIVE)


# --------------------------------------------------------------------------- #
# H4-b 任何策略下都恒拦：云元数据 / 链路本地 / 组播 / 保留
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    'url',
    [
        'http://169.254.169.254/latest/meta-data/iam/security-credentials/',
        'http://100.100.100.200/latest/meta-data/',
        'http://168.63.129.16/machine/?comp=goalstate',
        'http://[fd00:ec2::254]/latest/meta-data/',
        'http://0.0.0.0:8000/',
        'http://224.0.0.1/',
        'http://255.255.255.255/',
        'http://[::ffff:169.254.169.254]/latest/meta-data/',
    ],
)
@pytest.mark.parametrize('policy', [PERMISSIVE, STRICT])
def test_metadata_and_special_ranges_always_blocked(url, policy):
    with pytest.raises(SsrfBlocked):
        g.validate_url(url, policy)


# --------------------------------------------------------------------------- #
# H4-c 私网 / 回环：默认放行（保留内网测试能力），strict 收紧
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    'url',
    [
        'http://127.0.0.1:8000/api/health',
        'http://192.168.1.10:8080/api',
        'http://10.0.0.5/api',
        'http://172.16.0.9/api',
        'http://[::1]:8000/api',
    ],
)
def test_private_allowed_by_default(url):
    assert g.validate_url(url, PERMISSIVE) == url


@pytest.mark.parametrize(
    'url',
    [
        'http://127.0.0.1:8000/api/health',
        'http://192.168.1.10:8080/api',
        'http://10.0.0.5/api',
        'http://[::1]:8000/api',
    ],
)
def test_private_blocked_in_strict(url):
    with pytest.raises(SsrfBlocked):
        g.validate_url(url, STRICT)


def test_strict_blocks_internal_hostnames():
    for url in ('http://localhost:8000/', 'http://svc.internal/', 'http://a.local/'):
        with pytest.raises(SsrfBlocked):
            g.validate_url(url, STRICT)


# --------------------------------------------------------------------------- #
# H4-d 白名单：主机名精确命中 / CIDR 命中（strict 下放行）
# --------------------------------------------------------------------------- #
def test_allow_host_exact_in_strict():
    policy = SsrfPolicy(strict=True, allow_hosts=frozenset({'127.0.0.1'}))
    assert g.validate_url('http://127.0.0.1:9000/api', policy)


def test_allow_host_cidr_in_strict():
    policy = SsrfPolicy(strict=True, allow_hosts=frozenset({'10.0.0.0/8'}))
    assert g.validate_url('http://10.3.4.5/api', policy)


def test_allow_host_does_not_bypass_scheme():
    policy = SsrfPolicy(strict=True, allow_hosts=frozenset({'127.0.0.1'}))
    with pytest.raises(SsrfBlocked):
        g.validate_url('gopher://127.0.0.1:6379/_INFO', policy)


def test_unresolvable_host_blocked():
    with pytest.raises(SsrfBlocked):
        g.validate_url('http://no-such-host.invalid/api', PERMISSIVE)


# --------------------------------------------------------------------------- #
# H4-e 环境变量策略
# --------------------------------------------------------------------------- #
def test_env_strict_switch(monkeypatch):
    monkeypatch.setenv('API_TESTING_SSRF_STRICT', '1')
    monkeypatch.setenv('API_TESTING_SSRF_ALLOW_HOSTS', '10.0.0.0/8, localhost')
    monkeypatch.setenv('API_TESTING_SSRF_MAX_REDIRECTS', '2')
    g.reset_policy_cache()
    policy = g.get_policy()
    assert policy.strict is True
    assert '10.0.0.0/8' in policy.allow_hosts
    assert 'localhost' in policy.allow_hosts
    assert policy.max_redirects == 2


def test_env_bad_redirects_falls_back(monkeypatch):
    monkeypatch.setenv('API_TESTING_SSRF_MAX_REDIRECTS', 'not-a-number')
    g.reset_policy_cache()
    assert g.get_policy().max_redirects == g.DEFAULT_MAX_REDIRECTS


# --------------------------------------------------------------------------- #
# H4-f safe_request：重定向逐跳校验
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, status_code=200, headers=None, url='http://a/', text='ok',
                 content=b'ok'):
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url
        self.text = text
        self.content = content


def test_redirect_to_metadata_blocked(monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url))
        return _FakeResp(302, {'Location': 'http://169.254.169.254/latest/meta-data/'},
                         url=url)

    monkeypatch.setattr(g.requests, 'request', fake_request)
    with pytest.raises(SsrfBlocked):
        g.safe_request('GET', 'http://127.0.0.1:8000/redirect', policy=PERMISSIVE)
    assert calls == [('GET', 'http://127.0.0.1:8000/redirect')], '第二跳必须被拦在校验阶段'


def test_redirect_followed_when_target_safe(monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url))
        if len(calls) == 1:
            return _FakeResp(302, {'Location': '/v2/api'}, url=url)
        return _FakeResp(200, {}, url=url)

    monkeypatch.setattr(g.requests, 'request', fake_request)
    resp = g.safe_request('GET', 'http://127.0.0.1:8000/api', policy=PERMISSIVE)
    assert resp.status_code == 200
    assert calls == [
        ('GET', 'http://127.0.0.1:8000/api'),
        ('GET', 'http://127.0.0.1:8000/v2/api'),
    ]


def test_redirect_303_downgrades_to_get(monkeypatch):
    seen = []

    def fake_request(method, url, **kwargs):
        seen.append((method, url, 'json' in kwargs))
        if len(seen) == 1:
            return _FakeResp(303, {'Location': 'http://127.0.0.1:8000/done'}, url=url)
        return _FakeResp(200, {}, url=url)

    monkeypatch.setattr(g.requests, 'request', fake_request)
    g.safe_request('POST', 'http://127.0.0.1:8000/create', json={'a': 1},
                   policy=PERMISSIVE)
    assert seen[0] == ('POST', 'http://127.0.0.1:8000/create', True)
    assert seen[1] == ('GET', 'http://127.0.0.1:8000/done', False), '303 应降级为 GET 且丢弃 body'


def test_redirect_limit_stops_following(monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append(url)
        return _FakeResp(302, {'Location': f'http://127.0.0.1:8000/hop{len(calls)}'},
                         url=url)

    monkeypatch.setattr(g.requests, 'request', fake_request)
    resp = g.safe_request('GET', 'http://127.0.0.1:8000/0', policy=PERMISSIVE,
                          max_redirects=2)
    assert resp.status_code == 302
    assert len(calls) == 3, '首跳 + 2 次跟随后停止'


def test_safe_request_blocks_before_connect(monkeypatch):
    def boom(*a, **k):  # pragma: no cover - 不应被调用
        raise AssertionError('非法目标不应发起连接')

    monkeypatch.setattr(g.requests, 'request', boom)
    with pytest.raises(SsrfBlocked):
        g.safe_request('GET', 'http://169.254.169.254/latest/meta-data/',
                       policy=PERMISSIVE)


# --------------------------------------------------------------------------- #
# M16 日志脱敏
# --------------------------------------------------------------------------- #
def test_redact_url_masks_sensitive_query():
    url = ('https://qyapi.weixin.qq.com/cgi-bin/webhook/send'
           '?key=SUPER-SECRET&foo=bar&access_token=TTT')
    out = g.redact_url(url)
    assert 'SUPER-SECRET' not in out and 'TTT' not in out
    assert 'foo=bar' in out


def test_redact_url_strips_userinfo():
    out = g.redact_url('http://user:pass@example.com/api?sign=abc')
    assert 'user:pass' not in out
    assert 'abc' not in out


def test_mask_webhook_hides_path_and_query():
    for url in (
        'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=ABC123',
        'https://open.feishu.cn/open-apis/bot/v2/hook/1111-2222-uuid',
        'https://oapi.dingtalk.com/robot/send?access_token=XYZ',
    ):
        masked = g.mask_webhook_url(url)
        assert 'ABC123' not in masked
        assert 'uuid' not in masked
        assert 'XYZ' not in masked
        assert masked.endswith('/***')


# --------------------------------------------------------------------------- #
# H4/M16 接入点：出站请求已全部收口
# --------------------------------------------------------------------------- #
def test_outbound_calls_route_through_guard():
    """静态扫描 + 独立解释器验证接线。

    不在此进程内 import views：该模块的深层导入链会破坏 pytest 的
    capture 流（本仓库已知问题），改用子进程做「真实导入」验证更可靠。
    """
    import pathlib
    import subprocess
    import sys

    root = pathlib.Path(__file__).resolve().parents[1]
    for rel in ('apps/api_testing/views.py', 'apps/api_testing/utils.py'):
        src = (root / rel).read_text(encoding='utf-8')
        for banned in ('requests.request(', 'requests.post(', 'requests.get('):
            assert banned not in src, f'{rel} 仍存在直连 {banned}'

    code = (
        "import django\n"
        "django.setup()\n"
        "from apps.api_testing import ssrf_guard as g, utils, views\n"
        "assert utils.safe_request is g.safe_request\n"
        "assert views.safe_request is g.safe_request\n"
        "print('wired-ok')\n"
    )
    env = dict(os.environ)
    env['DJANGO_SETTINGS_MODULE'] = 'backend.settings'
    cp = subprocess.run(
        [sys.executable, '-c', code],
        cwd=str(root), env=env, capture_output=True, text=True, timeout=180,
    )
    assert cp.returncode == 0, cp.stderr[-2000:]
    assert 'wired-ok' in cp.stdout


def test_serializer_static_precheck_rejects_metadata():
    from rest_framework import serializers as drf_serializers

    from apps.api_testing.serializers import TaskNotificationSettingDetailSerializer

    validate = TaskNotificationSettingDetailSerializer.validate_custom_webhook_bots
    with pytest.raises(drf_serializers.ValidationError):
        validate(None, {'dingtalk': {'webhook_url': 'http://169.254.169.254/x'}})
    with pytest.raises(drf_serializers.ValidationError):
        validate(None, {'wechat': {'webhook_url': 'file:///etc/passwd'}})


def test_serializer_static_precheck_allows_normal_webhook():
    from apps.api_testing.serializers import TaskNotificationSettingDetailSerializer

    validate = TaskNotificationSettingDetailSerializer.validate_custom_webhook_bots
    payload = {
        'wechat': {'webhook_url': 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k'},
        'dingtalk': {'webhook_url': 'https://oapi.dingtalk.com/robot/send?access_token=t'},
        'empty': {},
    }
    assert validate(None, payload) == payload
    assert validate(None, None) is None
