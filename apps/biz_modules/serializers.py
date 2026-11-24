from rest_framework import serializers

from .models import BizModule


class BizModuleSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source="project.name", read_only=True)
    case_count = serializers.SerializerMethodField()

    class Meta:
        model = BizModule
        fields = [
            "id",
            "project",
            "project_name",
            "name",
            "namespace",
            "source",
            "description",
            "case_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_case_count(self, obj):
        return obj.cases.count()
