"""#182 站点冒烟巡检：纯逻辑单测（配置加载 / 单项结论 / 汇总 / 报告渲染 / 命令编排降级）。"""

import json
from datetime import datetime

import pytest
from django.core.management import call_command, get_commands

from apps.core import smoke_patrol

# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

class TestLoadSmokeConfig:
    def test_load_valid(self, tmp_path):
        cfg = tmp_path / 'smoke_tasks.json'
        cfg.write_text(json.dumps([
            {'name': 'A', 'task': '做A', 'enabled': True},
            {'name': 'B', 'task': '做B', 'enabled': False},
            {'name': 'C', 'task': '做C'},
        ], ensure_ascii=False), encoding='utf-8')
        items = smoke_patrol.load_smoke_config(cfg)
        assert [i['name'] for i in items] == ['A', 'C']  # enabled=false 被过滤

    def test_missing_file_returns_empty(self, tmp_path):
        assert smoke_patrol.load_smoke_config(tmp_path / 'nope.json') == []

    def test_broken_json_returns_empty(self, tmp_path):
        cfg = tmp_path / 'bad.json'
        cfg.write_text('{not json', encoding='utf-8')
        assert smoke_patrol.load_smoke_config(cfg) == []

    def test_non_list_returns_empty(self, tmp_path):
        cfg = tmp_path / 'obj.json'
        cfg.write_text('{"a": 1}', encoding='utf-8')
        assert smoke_patrol.load_smoke_config(cfg) == []

    def test_entries_without_task_skipped_and_named(self, tmp_path):
        cfg = tmp_path / 'cfg.json'
        cfg.write_text(json.dumps([
            {'name': '无任务'},
            {'task': '有任务无名'},
            'not-a-dict',
        ], ensure_ascii=False), encoding='utf-8')
        items = smoke_patrol.load_smoke_config(cfg)
        assert len(items) == 1
        assert items[0]['name'] == '巡检项2'


# ---------------------------------------------------------------------------
# 单项结论
# ---------------------------------------------------------------------------

class TestEvaluatePatrolItem:
    def test_passed(self):
        item = smoke_patrol.evaluate_patrol_item(
            'A', '做A', status='passed', duration=3.21,
            assertion_report={'quality_signal': 'pass', 'counts': {'passed': 2}}, log_tail='')
        assert item['conclusion'] == 'passed'
        assert item['assertion_signal'] == 'pass'
        assert item['duration'] == 3.2

    def test_executor_failed(self):
        item = smoke_patrol.evaluate_patrol_item(
            'A', '做A', status='failed', duration=1,
            assertion_report={'quality_signal': 'pass'}, log_tail='')
        assert item['conclusion'] == 'failed'

    def test_assertion_fail_downgrades_passed(self):
        item = smoke_patrol.evaluate_patrol_item(
            'A', '做A', status='passed', duration=1,
            assertion_report={'quality_signal': 'fail', 'counts': {'failed': 1}}, log_tail='')
        assert item['conclusion'] == 'failed'
        assert '断言' in item['note']

    def test_error_status(self):
        item = smoke_patrol.evaluate_patrol_item(
            'A', '做A', status='error', duration=0, assertion_report=None, log_tail='boom')
        assert item['conclusion'] == 'error'

    def test_stopped_status(self):
        item = smoke_patrol.evaluate_patrol_item(
            'A', '做A', status='stopped', duration=0, assertion_report=None, log_tail='')
        assert item['conclusion'] == 'stopped'

    def test_no_assertion_report_signal_none(self):
        item = smoke_patrol.evaluate_patrol_item(
            'A', '做A', status='passed', duration=1, assertion_report=None, log_tail='')
        assert item['assertion_signal'] == 'none'
        assert item['conclusion'] == 'passed'


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

class TestSummarize:
    def test_all_pass(self):
        s = smoke_patrol.summarize([{'conclusion': 'passed'}, {'conclusion': 'passed'}])
        assert s == {'total': 2, 'passed': 2, 'failed': 0, 'error': 0, 'stopped': 0, 'signal': 'pass'}

    def test_any_fail(self):
        s = smoke_patrol.summarize([{'conclusion': 'passed'}, {'conclusion': 'failed'}])
        assert s['signal'] == 'fail'

    def test_error_counts_as_fail(self):
        s = smoke_patrol.summarize([{'conclusion': 'error'}])
        assert s['signal'] == 'fail' and s['error'] == 1

    def test_empty(self):
        assert smoke_patrol.summarize([])['signal'] == 'none'


# ---------------------------------------------------------------------------
# 报告渲染与落盘
# ---------------------------------------------------------------------------

class TestReports:
    def _results(self):
        return [
            smoke_patrol.evaluate_patrol_item(
                '登录', '打开站点并登录', status='passed', duration=5.0,
                assertion_report={'quality_signal': 'pass', 'counts': {'passed': 2}},
                log_tail='step 1 ok\nstep 2 ok'),
            smoke_patrol.evaluate_patrol_item(
                '下单', '下单流程', status='passed', duration=8.0,
                assertion_report={'quality_signal': 'fail', 'counts': {'failed': 1}},
                log_tail='未见成功提示'),
        ]

    def test_markdown_contains_summary_and_items(self):
        results = self._results()
        summary = smoke_patrol.summarize(results)
        md = smoke_patrol.render_markdown_report(
            results, summary, datetime(2026, 11, 3, 10, 0, 0), 13.0)
        assert '# 站点冒烟巡检报告 2026-11-03 10:00:00' in md
        assert '**fail**' in md
        assert '✅ 登录' in md and '❌ 下单' in md
        assert '三层断言质量信号为 fail' in md  # 降级备注可见
        assert 'step 2 ok' in md  # 日志尾部进报告

    def test_write_reports(self, tmp_path):
        from pathlib import Path

        results = self._results()
        summary = smoke_patrol.summarize(results)
        paths = smoke_patrol.write_reports(
            results, summary, datetime(2026, 11, 3, 10, 0, 0), 13.0, tmp_path)
        assert paths['json'] and paths['md']
        data = json.loads(Path(paths['json']).read_text(encoding='utf-8'))
        assert data['summary']['signal'] == 'fail'
        assert len(data['items']) == 2
        assert '巡检报告 2026-11-03' in Path(paths['md']).read_text(encoding='utf-8')

    def test_write_reports_bad_dir_does_not_raise(self):
        paths = smoke_patrol.write_reports(
            [], smoke_patrol.summarize([]), datetime.now(), 0, 'Z:/不可能存在的盘符/x')
        assert paths == {'json': '', 'md': ''}


# ---------------------------------------------------------------------------
# 命令编排（mock 执行器，不真跑浏览器）
# ---------------------------------------------------------------------------

class TestCommandOrchestration:
    @pytest.mark.django_db
    def test_run_one_round_with_mock(self, tmp_path, monkeypatch):
        cfg = tmp_path / 'smoke_tasks.json'
        cfg.write_text(json.dumps([
            {'name': 'A', 'task': '做A'},
            {'name': 'B', 'task': '做B'},
        ], ensure_ascii=False), encoding='utf-8')
        out_dir = tmp_path / 'reports'

        calls = []

        class FakeHistory:
            steps = []

        class FakeAgent:
            _assertion_report = {'quality_signal': 'pass', 'counts': {}}

        def fake_run(task_description, analysis_callback=None, step_callback=None,
                     should_stop=None, case_name=None, agent_holder=None, **kw):
            calls.append(task_description)
            if analysis_callback:
                analysis_callback([{'id': 1, 'description': task_description, 'status': 'completed'}])
            agent_holder['agent'] = FakeAgent()
            return FakeHistory()

        monkeypatch.setattr(
            'apps.core.management.commands.run_smoke_patrol.run_full_process_sync', fake_run)

        call_command('run_smoke_patrol', config=str(cfg), out_dir=str(out_dir), interval=0)

        assert len(calls) == 2
        reports = list(out_dir.glob('smoke_patrol_*.json'))
        assert len(reports) == 1
        data = json.loads(reports[0].read_text(encoding='utf-8'))
        assert data['summary']['total'] == 2
        assert data['summary']['signal'] == 'pass'

    @pytest.mark.django_db
    def test_run_one_round_empty_config(self, tmp_path, monkeypatch):
        def fake_run(*a, **kw):  # pragma: no cover - 不应被调用
            raise AssertionError('空配置不应执行任何巡检项')

        monkeypatch.setattr(
            'apps.core.management.commands.run_smoke_patrol.run_full_process_sync', fake_run)

        call_command('run_smoke_patrol', config=str(tmp_path / 'missing.json'),
                     out_dir=str(tmp_path / 'r'), interval=0)

    def test_run_item_error_isolated(self, monkeypatch):
        def fake_run(*a, **kw):
            raise RuntimeError('LLM down')

        monkeypatch.setattr(
            'apps.core.management.commands.run_smoke_patrol.run_full_process_sync', fake_run)

        from apps.core.management.commands.run_smoke_patrol import Command
        cmd = Command()
        item = cmd.run_item('X', '做X')
        assert item['conclusion'] == 'error'

    def test_command_help_registered(self):
        assert get_commands().get('run_smoke_patrol') in ('core', 'apps.core')
