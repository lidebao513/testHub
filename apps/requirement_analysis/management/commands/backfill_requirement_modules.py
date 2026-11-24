"""回填历史需求的业务模块归属（任务 #453）。

新需求由 post_save 自动绑定；本命令只处理历史行。幂等，支持 --dry-run。
文档没有关联项目或 module 为空时跳过，绝不猜默认项目。
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandParser

from apps.requirement_analysis.models import BusinessRequirement
from apps.requirement_analysis.module_signals import bind_requirement_module


class Command(BaseCommand):
    help = "按 BusinessRequirement.module 回填 module_ref（namespace=requirement:<document_id>）"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
        parser.add_argument("--project", type=int, help="只回填该项目的需求")
        parser.add_argument("--limit", type=int, default=0, help="最多处理条数，0 表示不限")

    def handle(self, *args, **options) -> None:
        queryset = (
            BusinessRequirement.objects.filter(module_ref__isnull=True)
            .exclude(module="")
            .select_related("analysis__document")
        )
        if options["project"]:
            queryset = queryset.filter(analysis__document__project_id=options["project"])
        else:
            queryset = queryset.exclude(analysis__document__project__isnull=True)

        scanned = 0
        skipped = 0
        filled = 0
        for requirement in queryset:
            scanned += 1
            document = requirement.analysis.document
            if not document.project_id or not (requirement.module or "").strip():
                skipped += 1
                continue
            if options["dry_run"]:
                filled += 1
            else:
                before = requirement.module_ref_id
                bind_requirement_module(requirement)
                if requirement.module_ref_id and requirement.module_ref_id != before:
                    filled += 1
            if options["limit"] and filled >= options["limit"]:
                break

        label = "would fill" if options["dry_run"] else "filled"
        self.stdout.write(
            f"scanned={scanned} {label}={filled} skipped_no_project_or_module={skipped}"
        )
