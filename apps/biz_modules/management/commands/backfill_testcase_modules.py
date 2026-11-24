"""回填历史 testgen 用例的业务模块归属（幂等、可 dry-run）。

背景：PR #25 给 TestCase 加了 module FK，并让之后的回流自动挂模块；
历史用例的模块只留在 description 的「追溯：...; module=xxx; ...」文本里，
本命令把这些文本解析出来，按唯一键 (project, namespace, name) 建/找 BizModule 后回填。

用法：
    python manage.py backfill_testcase_modules --dry-run
    python manage.py backfill_testcase_modules --project 3 --testgen-project-id 42
    python manage.py backfill_testcase_modules --dry-run --limit 100

命名空间判定（避免裸 module 名跨项目/跨仓撞名）：
    1. 显式 --testgen-project-id 优先 → code:<id>（代码通道历史回流）；
    2. 项目恰有一条 UrlProject 绑定 → runtime:<归一URL>（地址通道回流）；
    3. 多条绑定（无法判定哪个地址产的用例）或多项目未给显式 id → 跳过并计数，
       不猜测，避免把用例挂到错误模块。

幂等：只处理 module_id 为空的用例；update 带 module_id__isnull=True 条件，
重复执行不会改写已有归属（含人工在页面上改过的归属）。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from apps.biz_modules.services import ensure_module, module_namespace
from apps.testcases.models import TestCase
from apps.testgen_integration.models import UrlProject

_TRACE_PREFIX = "追溯："


def _trace_module(description: str) -> str | None:
    """从 description 的追溯段落取 module=xxx；取不到返回 None。"""
    for line in (description or "").splitlines():
        if _TRACE_PREFIX not in line:
            continue
        segment = line.split(_TRACE_PREFIX, 1)[1]
        for part in segment.split(";"):
            part = part.strip()
            if part.startswith("module="):
                name = part[len("module=") :].strip()
                return name or None
    return None


class Command(BaseCommand):
    help = "把历史 testgen 用例 description 中的 module 追溯文本回填到 TestCase.module（支持 --dry-run）"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="只统计不写库")
        parser.add_argument("--project", type=int, default=0, help="只回填指定 testhub 项目 id")
        parser.add_argument(
            "--testgen-project-id",
            type=int,
            default=0,
            help="代码通道历史回流的 testgen 项目 id，用于 code:<id> 命名空间（优先于地址绑定）",
        )
        parser.add_argument("--limit", type=int, default=0, help="最多扫描多少条候选（0=不限）")

    def backfill(
        self,
        *,
        dry_run: bool = False,
        project_id: int = 0,
        testgen_project_id: int = 0,
        limit: int = 0,
    ) -> dict[str, int]:
        """执行回填并返回统计；与 handle 分离，便于直接单测。"""
        stats: Counter[str] = Counter()
        queryset = (
            TestCase.objects.filter(module_id__isnull=True).select_related("project").order_by("id")
        )
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        if limit > 0:
            queryset = queryset[:limit]

        # 按项目缓存命名空间判定结果，避免逐条查 UrlProject
        namespace_cache: dict[int, tuple[str, str] | None] = {}

        for case in queryset.iterator(chunk_size=500):
            stats["scanned"] += 1
            if "testgen" not in (case.tags or []):
                stats["skipped_not_testgen"] += 1
                continue

            name = _trace_module(case.description)
            if not name:
                stats["skipped_no_module"] += 1
                continue

            if case.project_id not in namespace_cache:
                namespace_cache[case.project_id] = self._namespace_for(
                    case.project_id, testgen_project_id
                )
            target = namespace_cache[case.project_id]
            if target is None:
                stats["skipped_ambiguous_namespace"] += 1
                continue
            namespace, source = target

            if dry_run:
                stats["would_fill"] += 1
                continue

            module = ensure_module(case.project_id, name, namespace, source)
            updated = TestCase.objects.filter(id=case.id, module_id__isnull=True).update(
                module_id=module.id
            )
            stats["filled"] += updated

        # 补齐零值键，调用方/输出与单测都能直接取确定字段
        for key in (
            "scanned",
            "skipped_not_testgen",
            "skipped_no_module",
            "skipped_ambiguous_namespace",
            "filled",
            "would_fill",
        ):
            stats.setdefault(key, 0)
        return dict(stats)

    @staticmethod
    def _namespace_for(project_id: int, testgen_project_id: int) -> tuple[str, str] | None:
        if testgen_project_id:
            return module_namespace("code", str(testgen_project_id)), "code"
        urls = list(
            UrlProject.objects.filter(testhub_project_id=project_id).values_list(
                "normalized_url", flat=True
            )
        )
        if len(urls) == 1:
            return module_namespace("runtime", urls[0]), "runtime"
        return None

    def handle(self, *args: Any, **options: Any) -> None:
        stats = self.backfill(
            dry_run=bool(options["dry_run"]),
            project_id=int(options["project"]),
            testgen_project_id=int(options["testgen_project_id"]),
            limit=int(options["limit"]),
        )
        prefix = "[dry-run] " if options["dry_run"] else ""
        filled_key = "would_fill" if options["dry_run"] else "filled"
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}回溯扫描 {stats.get('scanned', 0)} 条；"
                f"跳过：非 testgen {stats.get('skipped_not_testgen', 0)} / "
                f"无 module 追溯 {stats.get('skipped_no_module', 0)} / "
                f"来源不明 {stats.get('skipped_ambiguous_namespace', 0)}；"
                f"{'待回填' if options['dry_run'] else '已回填'} {stats.get(filled_key, 0)} 条"
            )
        )
