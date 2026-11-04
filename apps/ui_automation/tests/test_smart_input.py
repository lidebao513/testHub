"""#184 统一智能输入框测试：解析器矩阵 + parse 端点 + run_adhoc 组装。"""
from __future__ import annotations

import threading

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.ui_automation.smart_input import (
    compose_execution_input,
    compose_task_description,
    mask_password,
    parse_unified_input,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# 解析器矩阵
# ---------------------------------------------------------------------------

def test_parse_full_mixed_text():
    text = (
        'http://demo.todomvc.com\n'
        '账号: admin\n'
        '密码: Test@123456\n'
        '权限: 只读测试，禁止删除数据\n'
        '打开页面后添加三条任务\n'
        '验证：剩 0 条 active'
    )
    p = parse_unified_input(text)
    assert p['url'] == 'http://demo.todomvc.com'
    assert p['username'] == 'admin'
    assert p['password'] == 'Test@123456'
    assert '只读测试，禁止删除数据' in p['notes']
    assert any('添加三条任务' in a for a in p['actions'])
    assert p['asserts'] == ['剩 0 条 active']
    assert p['confidence'] == 'high'


def test_parse_single_line_three_segments():
    p = parse_unified_input('打开 http://demo.todomvc.com，添加三条任务，验证剩 0 条 active')
    assert p['url'] == 'http://demo.todomvc.com'
    assert any('添加三条任务' in a for a in p['actions'])
    assert p['asserts'] == ['剩 0 条 active']
    assert p['confidence'] == 'high'


def test_parse_bare_domain_fallback():
    p = parse_unified_input('demo.todomvc.com:8080/login 添加一条任务')
    assert p['url'] == 'demo.todomvc.com:8080/login'
    assert p['actions']


def test_parse_open_clause_with_url():
    p = parse_unified_input('打开 https://example.com/todo 然后勾选第一条')
    assert p['url'] == 'https://example.com/todo'
    assert any('勾选第一条' in a for a in p['actions'])


def test_parse_open_non_url_target_becomes_action():
    p = parse_unified_input('打开登录页，输入用户名')
    # 「登录页」不是 URL/域名 → 归动作，url 缺失 → low + warning
    assert p['url'] == ''
    assert any('打开登录页' in a for a in p['actions'])
    assert p['confidence'] == 'low'
    assert '未识别到目标 URL' in p['warnings']


def test_parse_english_labels():
    text = (
        'https://example.com\n'
        'username: alice\n'
        'password: secret99\n'
        'notes: read only'
    )
    p = parse_unified_input(text)
    assert p['username'] == 'alice'
    assert p['password'] == 'secret99'
    assert 'read only' in p['notes']


def test_parse_empty_and_garbage_fail_open():
    empty = parse_unified_input('')
    assert empty['url'] == ''
    assert empty['confidence'] == 'low'
    assert empty['raw'] == ''

    garbage = parse_unified_input('!!!@@@###')
    assert garbage['confidence'] == 'low'
    assert garbage['raw'] == '!!!@@@###'
    assert garbage['warnings']


def test_parse_actions_dedup_and_filler_strip():
    p = parse_unified_input('添加一条任务，然后添加一条任务，再刷新页面')
    assert p['actions'].count('添加一条任务') == 1
    assert '刷新页面' in p['actions']


def test_parse_never_raises():
    # 各类怪输入都不应抛错
    for text in (None, 123, '账号:', '密码: ', '验证：', '打开', '打开 '):
        p = parse_unified_input(text)  # type: ignore[arg-type]
        assert isinstance(p, dict)
        assert 'confidence' in p


# ---------------------------------------------------------------------------
# 打码与组装
# ---------------------------------------------------------------------------

def test_mask_password():
    assert mask_password('Test@123456') == 'Tes******56'
    assert mask_password('abc') == '******'
    assert mask_password('') == ''


def test_compose_persisted_excludes_password():
    parsed = {
        'url': 'http://x.com',
        'actions': ['添加任务'],
        'asserts': ['列表为空'],
        'username': 'admin',
        'password': 'SuperSecret1',
        'notes': ['只读'],
    }
    persisted = compose_task_description(parsed, include_credentials=False)
    agent = compose_task_description(parsed, include_credentials=True)

    assert 'SuperSecret1' not in persisted
    assert 'SuperSecret1' in agent
    assert 'admin' in persisted
    assert '打开 http://x.com' in persisted
    assert '验证：列表为空' in persisted
    assert '只读' in persisted


def test_compose_execution_input_parsed_priority_and_raw_fallback():
    raw = 'http://demo.com\n密码: RawPass123'
    parsed = {'url': '', 'username': 'admin', 'password': '', 'actions': ['下单'], 'asserts': [], 'notes': []}
    out = compose_execution_input(parsed, raw)
    # parsed 有 username/actions → 采用；password 缺失 → 从 raw 补；url raw 补
    assert out['parsed']['username'] == 'admin'
    assert out['parsed']['password'] == 'RawPass123'
    assert out['parsed']['url'] == 'http://demo.com'
    assert '下单' in out['parsed']['actions']
    assert 'RawPass123' not in out['persisted']
    assert 'RawPass123' in out['agent']


# ---------------------------------------------------------------------------
# parse 端点
# ---------------------------------------------------------------------------

def _api_client():
    U = get_user_model()
    user = U.objects.create(username='si_user')
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def test_parse_endpoint_requires_auth():
    client = APIClient()
    resp = client.post('/api/ui-automation/smart-input/parse/', {'text': 'x'}, format='json')
    assert resp.status_code in (401, 403)


def test_parse_endpoint_missing_text_400():
    resp = _api_client().post('/api/ui-automation/smart-input/parse/', {}, format='json')
    assert resp.status_code == 400


def test_parse_endpoint_masks_password():
    text = 'https://example.com\n账号: admin\n密码: SuperSecret1'
    resp = _api_client().post('/api/ui-automation/smart-input/parse/', {'text': text}, format='json')
    assert resp.status_code == 200
    body = resp.json()
    assert body['ok'] is True
    parsed = body['parsed']
    assert 'SuperSecret1' not in resp.content.decode()
    assert parsed['password_masked'] == 'Sup******t1'
    assert 'password' not in parsed
    assert 'SuperSecret1' not in parsed.get('task_description', '')  # 回填描述不含凭据
    assert parsed['task_description']  # 回填用描述已生成
    assert parsed['url'] == 'https://example.com'


# ---------------------------------------------------------------------------
# run_adhoc 组装（同步线程 + mock 执行，验证 persisted/agent 分离）
# ---------------------------------------------------------------------------

class _SyncThread:
    """测试用：把 Thread 换成同步执行，便于断言传参。"""

    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        self._target()


def test_run_adhoc_smart_input_composition(monkeypatch):
    from apps.ui_automation.models import AIExecutionRecord

    captured = {}

    def fake_run(task_description, **kwargs):
        captured['task_description'] = task_description

    monkeypatch.setattr('apps.ui_automation.ai_agent.run_full_process_sync', fake_run)
    monkeypatch.setattr(threading, 'Thread', _SyncThread)

    client = _api_client()
    payload = {
        'parsed': {
            'url': 'http://demo.com',
            'username': 'admin',
            'password': 'SuperSecret1',
            'actions': ['添加任务'],
            'asserts': ['列表为空'],
            'notes': ['只读'],
        },
        'enable_gif': False,
    }
    resp = client.post('/api/ui-automation/ai-execution-records/run_adhoc/', payload, format='json')
    assert resp.status_code == 200, resp.content

    record = AIExecutionRecord.objects.get(id=resp.json()['execution_id'])
    # 落库描述不含明文密码
    assert 'SuperSecret1' not in record.task_description
    assert 'admin' in record.task_description
    assert '打开 http://demo.com' in record.task_description
    # 执行线程拿到的是含凭据版本
    assert 'SuperSecret1' in captured['task_description']


def test_run_adhoc_legacy_path_unchanged(monkeypatch):
    """不传 parsed/smart_input 时行为与原先一致（描述原样落库与执行）。"""
    from apps.ui_automation.models import AIExecutionRecord

    captured = {}

    def fake_run(task_description, **kwargs):
        captured['task_description'] = task_description

    monkeypatch.setattr('apps.ui_automation.ai_agent.run_full_process_sync', fake_run)
    monkeypatch.setattr(threading, 'Thread', _SyncThread)

    client = _api_client()
    resp = client.post(
        '/api/ui-automation/ai-execution-records/run_adhoc/',
        {'task_description': '打开 http://a.com 检查标题'},
        format='json',
    )
    assert resp.status_code == 200
    record = AIExecutionRecord.objects.get(id=resp.json()['execution_id'])
    assert record.task_description == '打开 http://a.com 检查标题'
    assert captured['task_description'] == record.task_description
