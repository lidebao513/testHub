"""后台任务持久化模型。

设计要点：
- 这是「后台任务管理页」的唯一数据源，落库于 MySQL，刷新/重启 sidecar 都不丢；
- ``source`` 记录 sidecar 的 task_id，状态同步服务据此轮询 sidecar 刷新在途任务；
- ``is_closed`` 软关闭：关闭的任务默认不出现在列表，避免误删数据；
- ``payload_summary`` 仅存**掩码后**的请求摘要（与 testgen 红线一致，绝不存明文凭证）。
"""
from __future__ import annotations

import uuid

from django.db import models


def _uuid_hex() -> str:
    return uuid.uuid4().hex


class BackgroundTask(models.Model):
    TASK_TYPES = [
        ("generate", "代码生成"),
        ("sync", "用例回流"),
        ("verdict", "安全判定"),
        ("execute", "执行验证"),
        ("webhook_generate", "发布流水线·生成"),
        ("webhook_execute", "发布流水线·生成+执行"),
    ]
    STATUS_CHOICES = [
        ("pending", "等待中"),
        ("running", "运行中"),
        ("success", "成功"),
        ("failed", "失败"),
        ("cancelled", "已取消"),
        ("paused", "已暂停"),
    ]

    id = models.CharField(max_length=32, primary_key=True, default=_uuid_hex, editable=False)
    name = models.CharField(max_length=200, verbose_name="任务名称")
    task_type = models.CharField(max_length=24, choices=TASK_TYPES, default="generate", verbose_name="类型")
    # sidecar 任务 id；非 sidecar 任务（如同步回流）为空
    source = models.CharField(max_length=64, blank=True, default="", verbose_name="来源任务ID")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending", verbose_name="状态")
    progress = models.FloatField(default=0.0, verbose_name="进度(0~1)")
    stage = models.CharField(max_length=64, blank=True, default="", verbose_name="阶段")
    started_at = models.DateTimeField(null=True, blank=True, verbose_name="开始时间")
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name="结束时间")
    created_by = models.IntegerField(null=True, blank=True, verbose_name="创建人ID")
    created_by_username = models.CharField(max_length=64, blank=True, default="", verbose_name="创建人")
    # 掩码后的请求摘要（JSON 文本）
    payload_summary = models.TextField(blank=True, default="", verbose_name="请求摘要")
    result_summary = models.TextField(blank=True, default="", verbose_name="结果摘要")
    error = models.TextField(blank=True, default="", verbose_name="错误信息")
    is_closed = models.BooleanField(default=False, verbose_name="已关闭")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "后台任务"
        verbose_name_plural = "后台任务"

    def __str__(self) -> str:
        return f"{self.name} ({self.id[:8]}) [{self.status}]"
