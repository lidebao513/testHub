"""#182 AI 冒烟巡检定时任务：读取巡检配置，逐项用 browser-use 执行，汇总产出报告。

用法：
    python manage.py run_smoke_patrol                 # 单轮巡检（默认）
    python manage.py run_smoke_patrol --interval 3600 # 常驻循环（每小时一轮）
    python manage.py run_smoke_patrol --config path   # 指定配置文件

配置（data/smoke_tasks.json，示例见 data/smoke_tasks.example.json）：
    [{"name": "待办演示", "task": "打开 https://... 并添加一条待办后校验", "enabled": true}]

巡检项复用现有 browser-use 执行链路（#180 会话隔离、#181 三层断言），
每轮产出 JSON + Markdown 报告（data/smoke_patrol_reports/）。
"""

import logging
import time
from datetime import datetime

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from apps.core import smoke_patrol
from apps.ui_automation.ai_agent import run_full_process_sync

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'AI 冒烟巡检：按配置逐项执行站点关键路径走查，汇总产出巡检报告（#182）'

    def add_arguments(self, parser):
        parser.add_argument('--config', default=smoke_patrol.DEFAULT_CONFIG_PATH,
                            help='巡检配置 JSON 路径（默认 data/smoke_tasks.json）')
        parser.add_argument('--out-dir', default=smoke_patrol.DEFAULT_OUT_DIR,
                            help='报告输出目录（默认 data/smoke_patrol_reports）')
        parser.add_argument('--interval', type=int, default=0,
                            help='循环巡检间隔秒数；0 = 只跑一轮（默认）')

    def handle(self, *args, **options):
        config_path = options['config']
        out_dir = options['out_dir']
        interval = max(0, int(options['interval']))

        self.stdout.write(self.style.SUCCESS(
            f"{'=' * 60}\nAI 冒烟巡检（配置: {config_path}，输出: {out_dir}）\n{'=' * 60}"))

        while True:
            close_old_connections()
            try:
                self.run_one_round(config_path, out_dir)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                # 单轮失败不拖垮常驻循环
                logger.exception('巡检轮次执行出错: %s', e)
                self.stdout.write(self.style.ERROR(f'巡检轮次执行出错: {e}'))

            if interval <= 0:
                break
            self.stdout.write(f'等待 {interval} 秒后进行下一轮巡检...')
            time.sleep(interval)

    def run_one_round(self, config_path, out_dir):
        started_at = datetime.now()
        tasks = smoke_patrol.load_smoke_config(config_path)
        if not tasks:
            self.stdout.write(self.style.WARNING(
                '没有可执行的巡检项（配置缺失/为空/全部禁用）。'
                '可参考 data/smoke_tasks.example.json 创建配置。'))
            return

        self.stdout.write(f'本轮巡检项: {len(tasks)} 个')
        results = []
        for idx, entry in enumerate(tasks, 1):
            close_old_connections()
            name, task = entry['name'], entry['task']
            self.stdout.write(f'\n[{idx}/{len(tasks)}] {name}: {task[:80]}')
            results.append(self.run_item(name, task))

        duration = (datetime.now() - started_at).total_seconds()
        summary = smoke_patrol.summarize(results)
        paths = smoke_patrol.write_reports(results, summary, started_at, duration, out_dir)

        style = self.style.SUCCESS if summary['signal'] == 'pass' else self.style.ERROR
        self.stdout.write(style(
            f"\n巡检完成: {summary['passed']}/{summary['total']} 通过"
            f"（失败 {summary['failed']}，异常 {summary['error']}），耗时 {duration:.1f}s"))
        if paths.get('md'):
            self.stdout.write(f'报告: {paths["md"]}')

    def run_item(self, name, task):
        """执行单个巡检项。执行异常归为 error，不中断整轮。"""
        started = time.time()
        agent_holder = {}
        planned_holder = {}

        def on_analysis_complete(planned_tasks):
            # 任务分解结果在此捕获（history 上没有 planned_tasks）
            planned_holder['tasks'] = planned_tasks

        def should_stop():
            return False

        try:
            history = run_full_process_sync(
                task,
                analysis_callback=on_analysis_complete,
                step_callback=None,
                should_stop=should_stop,
                case_name=f'[冒烟巡检] {name}',
                agent_holder=agent_holder,
            )
        except Exception as e:
            logger.warning('巡检项执行异常: %s（%s）', name, e)
            return smoke_patrol.evaluate_patrol_item(
                name, task, status='error', duration=time.time() - started,
                assertion_report=None, log_tail=str(e))

        duration = time.time() - started
        status = 'passed'
        try:
            # 判定口径与执行器一致：子任务失败 or 步骤失败
            planned = planned_holder.get('tasks') or []
            if any(t.get('status') in ('failed', 'error') for t in planned):
                status = 'failed'
            else:
                steps = getattr(history, 'steps', None) or []
                if any(getattr(s, 'status', None) == 'failed' for s in steps):
                    status = 'failed'
        except Exception:
            pass

        agent = agent_holder.get('agent')
        assertion_report = getattr(agent, '_assertion_report', None)

        return smoke_patrol.evaluate_patrol_item(
            name, task, status=status, duration=duration,
            assertion_report=assertion_report if isinstance(assertion_report, dict) else None,
            log_tail='',
        )
