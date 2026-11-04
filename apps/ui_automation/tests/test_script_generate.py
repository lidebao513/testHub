"""#185 阶段二：脚本生成端点 + 草稿态落库 测试。

覆盖：鉴权、参数校验、安全拦截（云元数据/内网私有段）、预览产物结构、
凭据兜底打码、TestScript 草稿态创建往返。
生成逻辑用 monkeypatch 替换 GENERATE_FN，避免真实拉起 node/chromium。
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.ui_automation.models import TestScript as ScriptModel
from apps.ui_automation.models import UiProject
from apps.ui_automation.pw_mcp_client import is_safe_target_url

pytestmark = pytest.mark.django_db

URL = '/api/ui-automation/scripts/generate/'


def _api_client():
    U = get_user_model()
    user = U.objects.create(username='sg_user')
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


def _project(user):
    return UiProject.objects.create(name='sg_project', owner=user)


def test_generate_endpoint_requires_auth():
    client = APIClient()
    resp = client.post(URL, {'target_url': 'https://example.com'}, format='json')
    assert resp.status_code in (401, 403)


def test_generate_missing_target_url_400():
    client, _ = _api_client()
    resp = client.post(URL, {'project_id': 1}, format='json')
    assert resp.status_code == 400


def test_generate_missing_project_id_400():
    client, _ = _api_client()
    resp = client.post(URL, {'target_url': 'https://example.com'}, format='json')
    assert resp.status_code == 400


@pytest.mark.parametrize(
    'url',
    ['http://169.254.169.254/', 'http://192.168.1.50:8099/', 'http://10.0.0.5/'],
)
def test_generate_blocks_unsafe_url(url):
    assert is_safe_target_url(url) is False
    client, user = _api_client()
    resp = client.post(
        URL, {'target_url': url, 'project_id': _project(user).id}, format='json'
    )
    assert resp.status_code == 400


def test_generate_allows_public_url():
    assert is_safe_target_url('https://example.com') is True


def test_generate_success_returns_preview(monkeypatch):
    def fake_generate(target_url, instruction, language):
        return {
            'content': 'from playwright.sync_api import sync_playwright\n',
            'steps': ['navigate', 'snapshot', 'type', 'press_key'],
            'language': language,
        }

    monkeypatch.setattr(
        'apps.ui_automation.views_script_generate.GENERATE_FN', fake_generate
    )
    client, user = _api_client()
    resp = client.post(
        URL,
        {
            'target_url': 'https://example.com',
            'instruction': '在搜索框输入关键词',
            'project_id': _project(user).id,
            'language': 'python',
        },
        format='json',
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body['content'].startswith('from playwright')
    assert body['steps'] == ['navigate', 'snapshot', 'type', 'press_key']
    assert body['status'] == 'draft'  # preview/confirm：默认草稿态
    assert body['source'] == 'pw-mcp'
    assert body['framework'] == 'playwright'
    assert body['target_url'] == 'https://example.com'
    assert '在搜索框输入关键词' in body['instruction']


def test_generate_scrubs_secret_in_instruction(monkeypatch):
    seen = {}

    def fake_generate(target_url, instruction, language):
        # 引擎收到的必须是已打码的指令（否则会写进脚本的 fill(...)）
        seen['instruction'] = instruction
        return {
            'content': 'print("noop")\n',
            'steps': ['navigate'],
            'language': 'python',
        }

    monkeypatch.setattr(
        'apps.ui_automation.views_script_generate.GENERATE_FN', fake_generate
    )
    client, user = _api_client()
    resp = client.post(
        URL,
        {
            'target_url': 'https://example.com',
            'instruction': '账号 admin 密码 SuperSecret9 登录',
            'project_id': _project(user).id,
        },
        format='json',
    )
    assert resp.status_code == 200
    # 凭据绝不回显明文
    assert 'SuperSecret9' not in resp.content.decode()
    assert 'SuperSecret9' not in resp.json()['instruction']
    # 生成前已打码：引擎拿到的指令不含明文
    assert 'SuperSecret9' not in seen['instruction']
    assert 'admin' in seen['instruction']


def test_generate_scrubs_secret_in_generated_content(monkeypatch):
    """真实栈 E2E 实测缺陷回归：指令打码了，但脚本正文里的 fill(...) 仍带明文。"""

    def fake_generate(target_url, instruction, language):
        return {
            'content': (
                'page.get_by_role("textbox").fill("密码 SuperSecret9 登录")\n'
                "page.keyboard.press('Enter')\n"
            ),
            'steps': ['navigate', 'type'],
            'language': 'python',
        }

    monkeypatch.setattr(
        'apps.ui_automation.views_script_generate.GENERATE_FN', fake_generate
    )
    client, user = _api_client()
    resp = client.post(
        URL,
        {
            'target_url': 'https://example.com',
            'instruction': '正常指令',
            'project_id': _project(user).id,
        },
        format='json',
    )
    assert resp.status_code == 200
    body = resp.json()
    # content 必须已打码，明文不能进响应（否则会被前端落库）
    assert 'SuperSecret9' not in body['content']
    assert 'Su***9' in body['content']
    assert 'page.get_by_role("textbox").fill(' in body['content']


def test_script_create_accepts_draft_status(monkeypatch):
    """草稿态落库往返：生成预览后由前端以 status='draft' 创建，需可回读。"""
    client, user = _api_client()
    project = _project(user)
    resp = client.post(
        '/api/ui-automation/test-scripts/',
        {
            'project': project.id,
            'name': 'ai_draft_1',
            'script_type': 'CODE',
            'content': 'from playwright.sync_api import sync_playwright\n',
            'language': 'python',
            'framework': 'playwright',
            'status': 'draft',
        },
        format='json',
    )
    assert resp.status_code == 201
    sid = resp.json()['id']
    # 回读确认草稿态落库
    got = client.get(f'/api/ui-automation/test-scripts/{sid}/')
    assert got.status_code == 200
    assert got.json()['status'] == 'draft'
    assert ScriptModel.objects.get(id=sid).status == 'draft'
