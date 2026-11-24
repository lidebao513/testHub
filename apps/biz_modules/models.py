"""业务模块实体：用例的业务归属（批次 1，设计见 docs/business_module_entity_design_2026-11-24.md）。

与扫描清单 file_descriptions 正交：后者记录"扫到了什么"，本表记录"业务上怎么归类"。
唯一键 = (project, namespace, name)——namespace 带来源前缀，避免裸 module 名跨项目/跨仓撞名。
"""

from __future__ import annotations

from typing import ClassVar

from django.db import models

from apps.projects.models import Project


class BizModule(models.Model):
    """一个业务模块。source 仅作语义标记，唯一性由 namespace 保证。"""

    SOURCE_CHOICES: ClassVar[list] = [
        ("code", "代码扫描"),
        ("runtime", "运行时地址"),
        ("requirement", "需求文档"),
        ("manual", "手工创建"),
    ]

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="biz_modules",
        verbose_name="所属项目",
    )
    name = models.CharField(max_length=255, verbose_name="模块名称")
    namespace = models.CharField(
        max_length=500,
        default="",
        blank=True,
        verbose_name="来源命名空间",
        help_text="如 code:<repo_binding_id> / runtime:<归一URL> / requirement:<document_id>",
    )
    source = models.CharField(
        max_length=20,
        choices=SOURCE_CHOICES,
        default="manual",
        verbose_name="模块来源",
    )
    description = models.TextField(blank=True, default="", verbose_name="模块描述")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        db_table = "biz_modules"
        constraints: ClassVar[list] = [
            models.UniqueConstraint(
                fields=["project", "namespace", "name"],
                name="uq_biz_module_project_ns_name",
            )
        ]
        ordering: ClassVar[list] = ["source", "name"]
        verbose_name = "业务模块"
        verbose_name_plural = "业务模块"

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return f"{self.namespace}:{self.name}" if self.namespace else self.name
