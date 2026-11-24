from django.db import models
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, generics, permissions

from apps.projects.models import Project

from .models import BizModule
from .serializers import BizModuleSerializer


def _accessible_projects(user):
    return Project.objects.filter(models.Q(owner=user) | models.Q(members=user)).distinct()


class BizModuleListCreateView(generics.ListCreateAPIView):
    """模块列表/创建。仅返回用户有权限的项目下的模块。"""

    serializer_class = BizModuleSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["project", "source", "namespace"]
    search_fields = ["name", "description"]
    ordering_fields = ["name", "created_at"]
    ordering = ["source", "name"]

    def get_queryset(self):
        return BizModule.objects.filter(
            project__in=_accessible_projects(self.request.user)
        ).select_related("project").distinct()

    def perform_create(self, serializer):
        """创建时校验项目归属权限，防止跨项目挂模块。"""
        project = serializer.validated_data["project"]
        if not _accessible_projects(self.request.user).filter(id=project.id).exists():
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied("无权在该项目下创建模块")
        serializer.save()


class BizModuleDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = BizModuleSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return BizModule.objects.filter(
            project__in=_accessible_projects(self.request.user)
        ).select_related("project").distinct()
