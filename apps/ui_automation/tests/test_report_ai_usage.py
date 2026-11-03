"""AI 报告里的用量归因段（#178）离线自测。

关键语义只有一条：**成本未知必须表达为未知**。
cost=NULL 会被前端渲染成「未知」，而 0 会被读成「这次执行不花钱」——
把 NULL 洗成 0 等于伪造了一个好消息，所以这条要单独锁住。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.ui_automation.reports import AIExecutionReportGenerator


def _make_record(**overrides):
    """构造一个够报告生成器用的假记录（不碰数据库）。"""
    base = {
        'case_name': '登录冒烟',
        'planned_tasks': [{'id': 1, 'description': '打开登录页', 'status': 'completed'}],
        'logs': '',
        'steps_completed': [],
        'gif_path': None,
        'duration': 12.5,
        'start_time': None,
        'end_time': None,
        'status': 'passed',
        'total_tokens': None,
        'cost': None,
        'routed_model': '',
        'pre_explore': {},
        'get_status_display': lambda: '通过',
        'get_execution_mode_display': lambda: '临时执行',
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ============================================================================
# 成本三档口径
# ============================================================================


def test_cost_unknown_stays_none():
    """未配价目表且 provider 未回报 → None，不能变成 0。"""
    usage = AIExecutionReportGenerator(_make_record(cost=None))._generate_ai_usage()
    assert usage['cost'] is None
    assert usage['cost_known'] is False


def test_cost_zero_is_known():
    """provider 真报 0（例如命中缓存）与未知是两件事，要能区分。"""
    usage = AIExecutionReportGenerator(_make_record(cost=0))._generate_ai_usage()
    assert usage['cost'] == 0
    assert usage['cost_known'] is True


def test_cost_rounded_to_six_decimals():
    usage = AIExecutionReportGenerator(_make_record(cost=0.0012345678))._generate_ai_usage()
    assert usage['cost'] == 0.001235


@pytest.mark.parametrize('bad', ['', '0.1', None, [], {}])
def test_cost_non_numeric_falls_back_to_unknown(bad):
    usage = AIExecutionReportGenerator(_make_record(cost=bad))._generate_ai_usage()
    assert usage['cost'] is None
    assert usage['cost_known'] is False


# ============================================================================
# token / 模型
# ============================================================================


def test_total_tokens_passthrough():
    usage = AIExecutionReportGenerator(_make_record(total_tokens=46317))._generate_ai_usage()
    assert usage['total_tokens'] == 46317


@pytest.mark.parametrize('bad', [None, '46317', 4.5, True])
def test_total_tokens_invalid_is_none(bad):
    usage = AIExecutionReportGenerator(_make_record(total_tokens=bad))._generate_ai_usage()
    assert usage['total_tokens'] is None


def test_routed_model_blank_is_none():
    usage = AIExecutionReportGenerator(_make_record(routed_model='   '))._generate_ai_usage()
    assert usage['routed_model'] is None


def test_routed_model_trimmed():
    usage = AIExecutionReportGenerator(
        _make_record(routed_model=' deepseek-v4.1-flash ')
    )._generate_ai_usage()
    assert usage['routed_model'] == 'deepseek-v4.1-flash'


# ============================================================================
# 预探索快照
# ============================================================================


def test_pre_explore_empty():
    usage = AIExecutionReportGenerator(_make_record(pre_explore={}))._generate_ai_usage()
    assert usage['pre_explore'] == {
        'captured': False,
        'count': 0,
        'url': '',
        'title': '',
        'elements': [],
    }


def test_pre_explore_captured():
    record = _make_record(
        pre_explore={
            'url': 'http://127.0.0.1:3900/login',
            'title': '登录',
            'count': 2,
            'elements': [
                {'index': 1, 'tag': 'input', 'label': '用户名'},
                {'index': 2, 'tag': 'button', 'label': '登录'},
            ],
        }
    )
    pre = AIExecutionReportGenerator(record)._generate_ai_usage()['pre_explore']
    assert pre['captured'] is True
    assert pre['count'] == 2
    assert pre['url'].endswith('/login')
    assert [e['label'] for e in pre['elements']] == ['用户名', '登录']


def test_pre_explore_elements_truncated_to_20():
    """完整清单可能上百条，报告只回前 20 条。count 仍报真实总数。"""
    elements = [{'index': i, 'tag': 'button', 'label': f'按钮{i}'} for i in range(1, 26)]
    record = _make_record(pre_explore={'count': 25, 'elements': elements})
    pre = AIExecutionReportGenerator(record)._generate_ai_usage()['pre_explore']
    assert len(pre['elements']) == 20
    assert pre['count'] == 25


def test_pre_explore_non_dict_is_ignored():
    """历史脏数据（字符串/列表）不能让报告 500。"""
    for bad in ['oops', [], 3, None]:
        pre = AIExecutionReportGenerator(
            _make_record(pre_explore=bad)
        )._generate_ai_usage()['pre_explore']
        assert pre['captured'] is False
        assert pre['elements'] == []


# ============================================================================
# 三类报告都要带
# ============================================================================


@pytest.mark.parametrize(
    'method',
    ['generate_summary_report', 'generate_detailed_report', 'generate_performance_report'],
)
def test_all_reports_include_ai_usage(method):
    record = _make_record(
        total_tokens=1000,
        cost=0.002,
        routed_model='deepseek-v4.1-flash',
        pre_explore={'count': 1, 'elements': [{'index': 1, 'tag': 'a', 'label': 'x'}]},
        duration=12.5,
    )
    report = getattr(AIExecutionReportGenerator(record), method)()
    assert 'ai_usage' in report, f'{method} 缺少 ai_usage 段'
    usage = report['ai_usage']
    assert usage['routed_model'] == 'deepseek-v4.1-flash'
    assert usage['total_tokens'] == 1000
    assert usage['pre_explore']['count'] == 1
