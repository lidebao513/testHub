from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AICaseViewSet,
    AIExecutionRecordViewSet,
    ElementGroupViewSet,
    ElementViewSet,
    LocatorStrategyViewSet,
    OperationRecordViewSet,
    PageObjectViewSet,
    ScreenshotViewSet,
    ScriptStepViewSet,
    TestCaseExecutionViewSet,
    TestCaseStepViewSet,
    TestCaseViewSet,
    TestExecutionViewSet,
    TestScriptViewSet,
    TestSuiteViewSet,
    UiDashboardViewSet,
    UiNotificationLogViewSet,
    UiProjectViewSet,
    UiScheduledTaskViewSet,
)
from .views_closed_loop import ClosedLoopRunView, ClosedLoopStatusView
from .views_config import AIIntelligentModeConfigViewSet, EnvironmentConfigViewSet
from .views_smart_input import SmartInputParseView
from .views_script_generate import ScriptGenerateView

router = DefaultRouter()
router.register(r'dashboard', UiDashboardViewSet, basename='dashboard')
router.register(r'projects', UiProjectViewSet)
router.register(r'locator-strategies', LocatorStrategyViewSet)
router.register(r'element-groups', ElementGroupViewSet)
router.register(r'elements', ElementViewSet)
router.register(r'test-scripts', TestScriptViewSet)
router.register(r'page-objects', PageObjectViewSet)
router.register(r'steps', ScriptStepViewSet)
router.register(r'test-suites', TestSuiteViewSet)
router.register(r'test-executions', TestExecutionViewSet)
router.register(r'screenshots', ScreenshotViewSet)
router.register(r'test-cases', TestCaseViewSet)
router.register(r'test-case-steps', TestCaseStepViewSet)
router.register(r'test-case-executions', TestCaseExecutionViewSet)
router.register(r'scheduled-tasks', UiScheduledTaskViewSet)
router.register(r'ai-execution-records', AIExecutionRecordViewSet)
router.register(r'ai-cases', AICaseViewSet, basename='ai-cases')
router.register(r'ai-case-generation', AICaseViewSet, basename='ai-case-generation')
router.register(r'notification-logs', UiNotificationLogViewSet)
router.register(r'operation-records', OperationRecordViewSet)


# Configuration Center APIs
router.register(r'config/environment', EnvironmentConfigViewSet, basename='config-environment')
router.register(r'config/ai-mode', AIIntelligentModeConfigViewSet, basename='config-ai-mode')
router.register(r'ai-models', AIIntelligentModeConfigViewSet, basename='ai-models')

urlpatterns = [
    path('', include(router.urls)),
    # #200 闭环触发入口（异步）：UI 一键跑闭环 + 查状态
    path('closed-loop/run/', ClosedLoopRunView.as_view()),
    path('closed-loop/<str:task_id>/status/', ClosedLoopStatusView.as_view()),
    # #184 统一智能输入框：解析预览（只解析不执行）
    path('smart-input/parse/', SmartInputParseView.as_view()),
    # #185 阶段二：Playwright MCP 脚本生成（预览，不落库）
    path('scripts/generate/', ScriptGenerateView.as_view()),
]

# 添加媒体文件路由
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)