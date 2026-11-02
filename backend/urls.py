from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.conf.urls.static import static
from django.views.decorators.csrf import csrf_exempt
from apps.app_automation.static_views import protected_serve as serve
from apps.testgen_integration.proxy import (
    testgen_web_proxy,
    testgen_api_proxy,
)
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
    
    path('api/auth/', include('apps.users.urls')),
    path('api/projects/', include('apps.projects.urls')),
    path('api/testcases/', include('apps.testcases.urls')),
    path('api/testsuites/', include('apps.testsuites.urls')),
    path('api/executions/', include('apps.executions.urls')),
    path('api/reports/', include('apps.reports.urls')),
    path('api/reviews/', include('apps.reviews.urls')),
    path('api/versions/', include('apps.versions.urls')),
    path('api/defects/', include('apps.defects.urls')),
    path('api/assistant/', include('apps.assistant.urls')),
    path('api/users/', include('apps.users.urls')),
    path('api/requirement-analysis/', include('apps.requirement_analysis.urls')),
    path('api/ui-automation/', include('apps.ui_automation.urls')),
    path('api/app-automation/', include('apps.app_automation.urls')),  # APP自动化测试
    path('api/', include('apps.api_testing.urls')),
    path('api/core/', include('apps.core.urls')),
    path('api/data-factory/', include('apps.data_factory.urls')),
    path('api/llm-judge/', include('apps.llm_judge.urls')),
    path('api/perf-testing/', include('apps.perf_testing.urls')),  # 性能测试
    path('api/monitor/', include('apps.monitor.urls')),
    # MCP 管理端 REST（日志/待确认）；协议端点 /api/mcp/ 在 ASGI 层分流
    path('api/mcp/', include('apps.mcp.urls')),
    path('api/testgen/', include('apps.testgen_integration.urls')),  # testgen 代码生成 sidecar 集成
    # testgen 控制台反向代理（同源嵌入）
    #   页面：/testgen/              → sidecar 根（转发时注入 X-Testhub-Api-Prefix）
    #   API ：/testgen-api/v1/       → sidecar /api/v1/（专属命名空间，不与平台 /api/* 冲突）
    # 旧 /api/v1/ 保留兼容：靠归属判定挡住平台路径，观察期后下线。
    # 方案见 docs/testgen_api_prefix_plan_2026-10-30.md
    re_path(r'^testgen/(?P<rest>.*)$', testgen_web_proxy),
    re_path(r'^testgen$', testgen_web_proxy),
    re_path(
        r'^testgen-api/v1/(?P<rest>.*)$', testgen_api_proxy, {'dedicated': True}
    ),
    re_path(r'^api/v1/(?P<rest>.*)$', testgen_api_proxy),
]

if settings.ANALYTICS_ENABLED:
    urlpatterns.append(path('api/analytics/', include('apps.analytics.urls')))

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_FILES_URL, document_root=settings.STATIC_FILES_ROOT)

# APP自动化 Template 目录静态访问
# M3：原为裸 django.views.static.serve（无鉴权），现统一走 protected_serve：
# 必须已登录 + 路径穿越拒绝 + 扩展名白名单 + nosniff。
import os
urlpatterns += [
    path('app-automation-templates/<path:path>', 
         serve, 
         {'document_root': os.path.join(settings.BASE_DIR, 'apps', 'app_automation', 'Template')}),
]

# APP自动化 Allure 报告访问（同上，受保护）
urlpatterns += [
    path('app-automation-reports/<path:path>', 
         serve, 
         {'document_root': os.path.join(settings.MEDIA_ROOT, 'app-automation', 'allure-reports')}),
]
