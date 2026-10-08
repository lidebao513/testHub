"""任务中心 URL 配置（挂在 /api/taskcenter/）。"""
from django.urls import path

from .views import (
    TaskCloseView,
    TaskDetailView,
    TaskListView,
    TaskPauseView,
    TaskRetryView,
)

app_name = "taskcenter"

urlpatterns = [
    path("tasks/", TaskListView.as_view(), name="list"),
    path("tasks/<str:task_id>/", TaskDetailView.as_view(), name="detail"),
    path("tasks/<str:task_id>/retry/", TaskRetryView.as_view(), name="retry"),
    path("tasks/<str:task_id>/pause/", TaskPauseView.as_view(), name="pause"),
    path("tasks/<str:task_id>/close/", TaskCloseView.as_view(), name="close"),
]
