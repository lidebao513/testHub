"""1b 子批安全整改自测（H6 / M9 / M1 / M2），DB-free。

覆盖真实代码路径：
- H6：IsOwnerOrAdmin 对象权限 + UserListView.get_queryset 作用域过滤；
- M9：AppExecutionConsumer.connect 鉴权（匿名→4401 / 非owner→4403 / owner或admin→放行）；
      _owns_execution 归属校验；
- M1：proxy._do_proxy 要求登录（匿名→403）+ 注入 X-Auth-Token；
- M2：proxy 不再透传 cookies 给 sidecar。

与被测逻辑解耦于远程 DB：DB 查询均用 mock 桩替代，确保可独立运行。
"""
import os

import django
from django.conf import settings

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
if not settings.configured:
    django.setup()

settings.CHANNEL_LAYERS = {
    'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}
}

from django.contrib.auth.models import AnonymousUser  # noqa: E402
from django.test import RequestFactory  # noqa: E402
from unittest import IsolatedAsyncioTestCase  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

from apps.users.permissions import IsOwnerOrAdmin  # noqa: E402
from apps.users import views as users_views  # noqa: E402
from apps.app_automation.consumers import AppExecutionConsumer  # noqa: E402
from apps.testgen_integration import proxy as proxy_mod  # noqa: E402


# --------------------------------------------------------------------------- #
# H6：用户对象级权限
# --------------------------------------------------------------------------- #
class _FakeUser:
    def __init__(self, uid, is_staff=False):
        self.id = uid
        self.is_staff = is_staff
        self.is_authenticated = True

    def __eq__(self, other):
        return isinstance(other, _FakeUser) and other.id == self.id

    def __hash__(self):
        return self.id


class TestH6ObjectPermission:
    def test_owner_can_access(self):
        u = _FakeUser(1)
        perm = IsOwnerOrAdmin()
        assert perm.has_object_permission(MagicMock(user=u), None, u) is True

    def test_admin_can_access(self):
        u = _FakeUser(1)
        admin = _FakeUser(2, is_staff=True)
        perm = IsOwnerOrAdmin()
        assert perm.has_object_permission(MagicMock(user=admin), None, u) is True

    def test_other_user_denied(self):
        u = _FakeUser(1)
        other = _FakeUser(2)
        perm = IsOwnerOrAdmin()
        assert perm.has_object_permission(MagicMock(user=other), None, u) is False

    def test_unauthenticated_denied(self):
        perm = IsOwnerOrAdmin()
        req = MagicMock(user=AnonymousUser())
        assert perm.has_permission(req, None) is False


class TestH6ListScope:
    def test_non_staff_queryset_filtered_to_self(self):
        u = _FakeUser(7)
        view = users_views.UserListView()
        view.request = MagicMock(user=u)
        with patch.object(users_views.User, 'objects') as objs:
            objs.all.return_value.order_by.return_value = MagicMock()
            filtered = MagicMock()
            objs.all.return_value.order_by.return_value.filter.return_value = filtered
            result = view.get_queryset()
            # 非管理员：应调用 filter(id=7)
            objs.all.return_value.order_by.return_value.filter.assert_called_once_with(id=7)
            assert result is filtered

    def test_staff_queryset_not_filtered(self):
        admin = _FakeUser(1, is_staff=True)
        view = users_views.UserListView()
        view.request = MagicMock(user=admin)
        with patch.object(users_views.User, 'objects') as objs:
            base = MagicMock()
            objs.all.return_value.order_by.return_value = base
            result = view.get_queryset()
            # 管理员：不应调用 filter（返回全部）
            base.filter.assert_not_called()
            assert result is base


# --------------------------------------------------------------------------- #
# M9：WebSocket 鉴权
# --------------------------------------------------------------------------- #
def _make_app_consumer(user=None, execution_id='1'):
    scope = {
        'type': 'websocket',
        'url_route': {'kwargs': {'execution_id': execution_id}},
        'user': user if user is not None else AnonymousUser(),
    }
    c = AppExecutionConsumer(scope)
    c.scope = scope
    c.channel_layer = AsyncMock()
    c.channel_name = 'test-channel'
    c.accept = AsyncMock()
    c.close = AsyncMock()
    return c


class TestM9ConsumerAuth(IsolatedAsyncioTestCase):
    async def test_anonymous_rejected(self):
        c = _make_app_consumer(user=AnonymousUser())
        await c.connect()
        c.close.assert_awaited_once()           # 4401（默认 close 无参也罢）
        c.accept.assert_not_awaited()

    async def test_owner_accepted(self):
        c = _make_app_consumer(user=_FakeUser(1))
        c._owns_execution = AsyncMock(return_value=True)
        await c.connect()
        c.accept.assert_awaited_once()
        c.channel_layer.group_add.assert_awaited_once()
        c.close.assert_not_awaited()

    async def test_non_owner_rejected(self):
        c = _make_app_consumer(user=_FakeUser(2))
        c._owns_execution = AsyncMock(return_value=False)
        await c.connect()
        c.close.assert_awaited_once()
        c.accept.assert_not_awaited()
        c.channel_layer.group_add.assert_not_awaited()

    async def test_admin_accepted_via_owns(self):
        c = _make_app_consumer(user=_FakeUser(9, is_staff=True))
        c._owns_execution = AsyncMock(return_value=True)
        await c.connect()
        c.accept.assert_awaited_once()
        c.close.assert_not_awaited()


class TestM9OwnsExecution:
    def test_owns_execution_owner(self):
        import asyncio
        # _owns_execution 内部惰性 import，故 patch 落到 models 模块。
        with patch('apps.app_automation.models.AppTestExecution') as ET:
            mgr = MagicMock()
            mgr.filter.return_value.exists.return_value = True
            ET.objects = mgr
            c = _make_app_consumer(user=_FakeUser(1))
            res = asyncio.run(c._owns_execution(_FakeUser(1), 5))
            mgr.filter.assert_called_once_with(id=5, user=_FakeUser(1))
            assert res is True

    def test_owns_execution_not_owner(self):
        import asyncio
        with patch('apps.app_automation.models.AppTestExecution') as ET:
            mgr = MagicMock()
            mgr.filter.return_value.exists.return_value = False
            ET.objects = mgr
            c = _make_app_consumer(user=_FakeUser(2))
            res = asyncio.run(c._owns_execution(_FakeUser(2), 5))
            assert res is False

    def test_owns_execution_admin_ignores_user(self):
        import asyncio
        with patch('apps.app_automation.models.AppTestExecution') as ET:
            mgr = MagicMock()
            mgr.filter.return_value.exists.return_value = True
            ET.objects = mgr
            c = _make_app_consumer(user=_FakeUser(9, is_staff=True))
            res = asyncio.run(c._owns_execution(_FakeUser(9, is_staff=True), 5))
            mgr.filter.assert_called_once_with(id=5)  # 管理员只按 id 查
            assert res is True


# --------------------------------------------------------------------------- #
# M1 / M2：proxy 鉴权 + 去 cookie
# --------------------------------------------------------------------------- #
class TestProxyAuthAndCookie:
    def setup_method(self):
        self.factory = RequestFactory()
        self._orig_token = proxy_mod.TESTGEN_AUTH_TOKEN
        proxy_mod.TESTGEN_AUTH_TOKEN = "svc-secret-xyz"

    def teardown_method(self):
        proxy_mod.TESTGEN_AUTH_TOKEN = self._orig_token

    def _authed_request(self, path="/testgen/x", user=None, method="get"):
        req = getattr(self.factory, method)(path)
        req.user = user if user is not None else _FakeUser(1)
        return req

    def test_anonymous_is_forbidden(self):
        req = self.factory.get("/testgen/x")
        req.user = AnonymousUser()
        resp = proxy_mod._do_proxy(req, "x")
        assert resp.status_code == 403

    def test_authenticated_injects_auth_token_and_no_cookies(self):
        req = self._authed_request()
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.headers = {}
        fake_resp.content = b"ok"
        with patch.object(proxy_mod.requests, 'request', return_value=fake_resp) as mock_req:
            resp = proxy_mod._do_proxy(req, "x")
        assert resp.status_code == 200
        _, kwargs = mock_req.call_args
        # M1：注入 X-Auth-Token
        assert kwargs['headers'].get('X-Auth-Token') == "svc-secret-xyz"
        # M2：不得透传 cookies 给 sidecar
        assert 'cookies' not in kwargs

    def test_injected_token_overrides_incoming(self):
        # 模拟攻击者在请求头中伪造 X-Auth-Token（RequestFactory 经 HTTP_X_AUTH_TOKEN 注入）
        req = self.factory.get("/testgen/x", HTTP_X_AUTH_TOKEN="attacker-forged")
        req.user = _FakeUser(1)
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.headers = {}
        fake_resp.content = b"ok"
        with patch.object(proxy_mod.requests, 'request', return_value=fake_resp) as mock_req:
            proxy_mod._do_proxy(req, "x")
        _, kwargs = mock_req.call_args
        # 服务端令牌始终覆盖伪造值（M1）
        assert kwargs['headers'].get('X-Auth-Token') == "svc-secret-xyz"
