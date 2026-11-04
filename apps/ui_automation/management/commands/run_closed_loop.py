"""#183 生成→执行→回流 闭环命令：读取作业配置，逐项派发执行→断言→回流，产出报告。

用法：
    python manage.py run_closed_loop                       # 按 data/closed_loop.json 跑（需 enabled=true）
    python manage.py run_closed_loop --config path          # 指定配置
    python manage.py run_closed_loop --project-id 3         # 指定项目下全部 AICase
    python manage.py run_closed_loop --case-ids 1,2,3       # 指定若干 AICase
    python manage.py run_closed_loop --dry-run              # 不启动浏览器，验证闭环编排本身
    python manage.py run_closed_loop --force                # 忽略配置的 enabled=false

配置（data/closed_loop.json，示例见 data/closed_loop.example.json）：
    {"enabled": false, "max_retries": 1, "case_ids": [1,2,3], "project_id": null}

闭环：每个 AICase → 派发(建 AIExecutionRecord) → 执行(run_full_process_sync) →
三层断言信号 → 回流写 AICase.last_loop + 本轮 JSON/MD 报告。仅对 infra error 重试。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from apps.ui_automation.closed_loop import (
    ClosedLoopOrchestrator,
    build_loop_report,
    execute_aicase_real,
    reflow_aicase_real,
    render_loop_report_md,
    summarize_loop,
)

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "data/closed_loop.json"
DEFAULT_OUT_DIR = "data/closed_loop_reports"


class Command(BaseCommand):
    help = '生成→执行→回流 闭环：派发 AICase 执行并回流判定，产出闭环报告（#183）'

    def add_arguments(self, parser):
        parser.add_argument('--config', default=DEFAULT_CONFIG_PATH,
                            help='作业配置 JSON 路径（默认 data/closed_loop.json）')
        parser.add_argument('--out-dir', default=DEFAULT_OUT_DIR,
                            help='报告输出目录（默认 data/closed_loop_reports）')
        parser.add_argument('--project-id', type=int, default=None,
                            help='指定项目下全部 AICase（覆盖配置）')
        parser.add_argument('--case-ids', type=str, default=None,
                            help='逗号分隔的 AICase id 列表（覆盖配置）')
        parser.add_argument('--max-retries', type=int, default=None,
                            help='对 infra error 的最大重试次数（覆盖配置）')
        parser.add_argument('--dry-run', action='store_true',
                            help='不启动浏览器，验证闭环编排本身（执行直接标记通过）')
        parser.add_argument('--force', action='store_true',
                            help='忽略配置 enabled=false，强制运行')

    def handle(self, *args, **options):
        config_path = options['config']
        out_dir = options['out_dir']
        cfg = self.load_config(config_path)

        enabled = bool(cfg.get('enabled', False))
        if not enabled and not options['force']:
            self.stdout.write(self.style.WARNING(
                '闭环未启用（config.enabled=false 且未加 --force）。'
                '参考 data/closed_loop.example.json 配置后运行。'))
            return

        # 作业来源：CLI 优先，其次配置
        case_ids = self.parse_ids(options['case_ids']) or self.parse_ids(cfg.get('case_ids'))
        project_id = options['project_id'] if options['project_id'] is not None else cfg.get('project_id')
        max_retries = options['max_retries'] if options['max_retries'] is not None else int(cfg.get('max_retries', 1))

        jobs = self.build_jobs(case_ids=case_ids, project_id=project_id)
        if not jobs:
            self.stdout.write(self.style.WARNING(
                '没有可闭环的 AICase（既无 case_ids 也无 project_id，或对应用例不存在）。'))
            return

        self.stdout.write(self.style.SUCCESS(
            f"{'=' * 60}\n生成→执行→回流 闭环（作业 {len(jobs)} 个，dry_run={options['dry_run']}，"
            f"max_retries={max_retries}）\n{'=' * 60}"))

        started_at = datetime.now()
        orchestrator = ClosedLoopOrchestrator(
            execute_fn=lambda job: execute_aicase_real(
                self.case_by_id(job['case_id']), dry_run=options['dry_run']),
            reflow_fn=reflow_aicase_real,
            max_retries=max_retries,
            on_item=lambda it: self.stdout.write(
                f"  [{it.case_id}] {it.name}: {it.state.value} (verdict={it.verdict})"),
        )
        items = orchestrator.run(jobs)
        duration = (datetime.now() - started_at).total_seconds()
        summary = summarize_loop(items)
        report = build_loop_report(items, summary, started_at, duration)

        paths = self.write_reports(report, out_dir)
        style = self.style.SUCCESS if summary['signal'] in ('pass', 'warn') else self.style.ERROR
        self.stdout.write(style(
            f"\n闭环完成: 通过 {summary['done']}/{summary['total']}"
            f"（告警 {summary['warn']}，失败 {summary['failed']}，异常 {summary['error']}，"
            f"跳过 {summary['skipped']}），耗时 {duration:.1f}s，信号 {summary['signal']}"))
        if paths.get('md'):
            self.stdout.write(f'报告: {paths["md"]}')

    # ---- 辅助 ----

    def load_config(self, config_path: str) -> dict:
        p = Path(config_path)
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding='utf-8')) or {}
        except Exception as e:  # noqa: BLE001
            logger.warning('读取闭环配置失败（按空配置）: %s', e)
            return {}

    @staticmethod
    def parse_ids(raw) -> list[int]:
        if not raw:
            return []
        if isinstance(raw, list):
            ids = raw
        else:
            ids = [x.strip() for x in str(raw).split(',') if x.strip()]
        out = []
        for x in ids:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                continue
        return out

    def build_jobs(self, *, case_ids: list[int], project_id=None) -> list[dict]:
        from apps.ui_automation.models import AICase

        close_old_connections()
        qs = AICase.objects.all()
        if case_ids:
            qs = qs.filter(id__in=case_ids)
        elif project_id:
            qs = qs.filter(project_id=project_id)
        else:
            return []
        jobs = []
        for case in qs:
            jobs.append({
                'case_id': case.id,
                'name': case.name,
                'task_description': case.task_description,
            })
        return jobs

    def case_by_id(self, case_id):
        from apps.ui_automation.models import AICase

        close_old_connections()
        return AICase.objects.filter(id=case_id).first()

    def write_reports(self, report: dict, out_dir: str) -> dict:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        json_path = out / f'closed_loop_{ts}.json'
        md_path = out / f'closed_loop_{ts}.md'
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        md_path.write_text(render_loop_report_md(report), encoding='utf-8')
        return {'json': str(json_path), 'md': str(md_path)}
