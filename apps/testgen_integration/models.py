"""地址优先入口的项目映射（新功能独立模型，不改动现有 Project 结构）。"""

from __future__ import annotations

from typing import ClassVar

from django.db import models

from apps.projects.models import Project


class UrlProject(models.Model):
    """一条归一被测地址 ↔ 两侧项目 id 的绑定。

    - 归一 URL 是唯一键：同地址永远只对应一组项目；
    - testhub 侧用真实 FK（可回流用例），sidecar 侧只存整数 id（跨库无法 FK）。
    """

    normalized_url = models.CharField(
        max_length=255, unique=True, verbose_name="归一化被测地址"
    )
    scheme = models.CharField(max_length=16, default="http", verbose_name="协议")
    host = models.CharField(max_length=255, verbose_name="主机")
    testhub_project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="url_bindings",
        verbose_name="TestHub 项目",
    )
    testgen_project_id = models.PositiveIntegerField(verbose_name="testgen 项目 ID")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        db_table = "url_projects"
        ordering: ClassVar[list] = ["-created_at"]
        verbose_name = "地址项目映射"
        verbose_name_plural = "地址项目映射"

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return self.normalized_url
