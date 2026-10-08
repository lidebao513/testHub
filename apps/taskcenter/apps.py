"""任务中心应用：统一承载平台内异步/后台任务的持久化与运维操作。"""
from django.apps import AppConfig


class TaskcenterConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.taskcenter"
    label = "taskcenter"
    verbose_name = "后台任务中心"
