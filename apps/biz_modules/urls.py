from django.urls import path

from .views import BizModuleDetailView, BizModuleListCreateView

urlpatterns = [
    path("", BizModuleListCreateView.as_view(), name="biz-module-list"),
    path("<int:pk>/", BizModuleDetailView.as_view(), name="biz-module-detail"),
]
