from django.apps import AppConfig


class RequirementAnalysisConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.requirement_analysis'
    verbose_name = '需求分析'

    def ready(self):
        # 需求保存时把 module 字符串挂到 BizModule（namespace=requirement:<document_id>）
        from . import module_signals  # noqa: F401