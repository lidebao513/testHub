"""后台任务序列化器。"""
from rest_framework import serializers

from .models import BackgroundTask


class BackgroundTaskSerializer(serializers.ModelSerializer):
    class Meta:
        model = BackgroundTask
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]
