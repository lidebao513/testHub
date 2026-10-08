"""G2：启动/CI 自检 testgen sidecar 可达性。

用法：
    python manage.py check_testgen_sidecar

退出码：
    0  sidecar 可达
    1  sidecar 不可达（用于启动脚本 / CI 门禁拦截）
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.testgen_integration.client import base_url, is_sidecar_available


class Command(BaseCommand):
    help = "G2：探活 testgen sidecar；不可达时退出码 1，便于启动脚本与 CI 校验。"

    def handle(self, *args, **options) -> None:
        ok, detail = is_sidecar_available()
        if ok:
            self.stdout.write(
                self.style.SUCCESS(
                    f"[OK] testgen sidecar 可达：{base_url()} （{detail}）"
                )
            )
            return
        self.stderr.write(
            self.style.ERROR(
                f"[FAIL] testgen sidecar 不可达：{base_url()} -> {detail}\n"
                f"       请确认 sidecar 已启动（如 services/testgen 的 serve 命令）且 TESTGEN_BASE_URL 配置正确。"
            )
        )
        raise SystemExit(1)
