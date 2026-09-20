"""testgen 集成 URL 配置（挂在 /api/testgen/）。"""
from __future__ import annotations

from django.urls import path

from .views import TestgenGenerateView, TestgenSyncView

app_name = "testgen_integration"

urlpatterns = [
    path("generate", TestgenGenerateView.as_view(), name="generate"),
    path("sync", TestgenSyncView.as_view(), name="sync"),
]
