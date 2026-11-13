import json
import logging
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import models
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import (
    AIServiceConfig,
    ApiCollection,
    ApiProject,
    ApiRequest,
    ApiSpecImport,
    Environment,
    GeneratedApiTestCase,
    NotificationLog,
    OperationLog,
    RequestHistory,
    ScheduledTask,
    TaskExecutionLog,
    TaskNotificationSetting,
    TestExecution,
    TestSuite,
    TestSuiteRequest,
)
from .serializers import (
    ApiCollectionSerializer,
    ApiProjectSerializer,
    ApiRequestSerializer,
    EnvironmentSerializer,
    NotificationLogDetailSerializer,
    NotificationLogSerializer,
    OperationLogSerializer,
    RequestHistorySerializer,
    ScheduledTaskSerializer,
    TaskExecutionLogSerializer,
    TaskNotificationSettingDetailSerializer,
    TaskNotificationSettingSerializer,
    TestExecutionSerializer,
    TestSuiteRequestSerializer,
    TestSuiteSerializer,
    UserSerializer,
)

logger = logging.getLogger(__name__)



from .operation_logger import log_operation
from .serializers import (
    AIServiceConfigSerializer,
    ApiSpecImportSerializer,
    GeneratedApiTestCaseSerializer,
)
from .services import (
    ApiTestCaseAgent,
    FailureAttributionService,
    OpenApiImportService,
)
from .ssrf_guard import SsrfBlocked, mask_webhook_url, safe_request
from .utils import (
    apply_auth,
    execute_assertions,
    read_response_body,
    read_response_json,
)
from .variable_resolver import VariableResolver

# ---------------------------------------------------------------------------
# L8：定时任务派发（Celery 优先，兜底为**有界**线程池）
#
# 改造前：`run_now` 每个请求都 `threading.Thread(daemon=True).start()`，线程数
# 无上限（放大 H4 SSRF）；改造后：主路径进 Celery（并发度由 worker 决定），
# broker 不可用时退回固定大小的线程池，池满直接拒绝（429）而不是无限堆积。
# ---------------------------------------------------------------------------
_FALLBACK_MAX_WORKERS = max(1, int(getattr(settings, 'SCHEDULED_TASK_MAX_WORKERS', 4) or 4))
_FALLBACK_POOL = ThreadPoolExecutor(max_workers=_FALLBACK_MAX_WORKERS)
_FALLBACK_SEM = threading.BoundedSemaphore(_FALLBACK_MAX_WORKERS)


class TaskQueueFull(Exception):
    """后台执行队列已满：兜底池所有槽位被占用。"""


def _dispatch_scheduled_task(task, execution_log, user_id=None):
    """提交定时任务执行，返回 `(dispatch_mode, celery_task_id)`。

    `dispatch_mode` 取值：`celery`（已入队）/ `thread`（兜底池）。
    broker 不可用且池已满时抛 `TaskQueueFull`，由视图转成 429。
    """
    try:
        from .tasks import execute_scheduled_task

        async_result = execute_scheduled_task.delay(task.id, execution_log.id, user_id)
        return 'celery', getattr(async_result, 'id', None)
    except Exception as exc:
        logger.warning(
            "Celery 派发失败，退回有界线程池：task=%s err=%s", task.id, type(exc).__name__
        )

    if not _FALLBACK_SEM.acquire(blocking=False):
        raise TaskQueueFull('后台执行队列已满，请稍后重试')

    def _wrapped():
        try:
            ScheduledTaskViewSet()._run_task_sync(task.id, execution_log.id)
        except Exception:  # 兜底线程内异常必须吞掉，避免静默丢日志
            logger.exception("兜底线程执行定时任务失败：task=%s", task.id)
        finally:
            _FALLBACK_SEM.release()

    _FALLBACK_POOL.submit(_wrapped)
    return 'thread', None


User = get_user_model()


from rest_framework.pagination import PageNumberPagination


class StandardPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 1000


class ApiProjectViewSet(viewsets.ModelViewSet):
    queryset = ApiProject.objects.all()
    serializer_class = ApiProjectSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['project_type', 'status', 'owner']
    search_fields = ['name', 'description']
    ordering_fields = ['created_at', 'name', 'start_date']
    ordering = ['-created_at']
    
    def get_queryset(self):
        user = self.request.user
        return ApiProject.objects.filter(
            models.Q(owner=user) | models.Q(members=user)
        ).distinct()
    
    def perform_create(self, serializer):
        """创建项目时记录日志"""
        instance = serializer.save()
        log_operation(
            operation_type='create',
            resource_type='project',
            resource_id=instance.id,
            resource_name=instance.name,
            user=self.request.user
        )
    
    def perform_update(self, serializer):
        """更新项目时记录日志"""
        instance = serializer.save()
        log_operation(
            operation_type='edit',
            resource_type='project',
            resource_id=instance.id,
            resource_name=instance.name,
            user=self.request.user
        )
    
    def perform_destroy(self, instance):
        """删除项目时记录日志"""
        log_operation(
            operation_type='delete',
            resource_type='project',
            resource_id=instance.id,
            resource_name=instance.name,
            user=self.request.user
        )
        instance.delete()
    
    @action(detail=False, methods=['post'], url_path='create-sample')
    def create_sample_project(self, request):
        """创建示例项目（宠物店）"""
        if ApiProject.objects.filter(name='宠物店API示例项目').exists():
            return Response({'message': '示例项目已存在'}, status=status.HTTP_400_BAD_REQUEST)
        
        # 创建示例项目
        project = ApiProject.objects.create(
            name='宠物店API示例项目',
            description='参考Apifox宠物店示例，包含用户管理、宠物管理、订单管理等接口',
            project_type='HTTP',
            status='IN_PROGRESS',
            owner=request.user,
            start_date=datetime.now().date()
        )
        
        # 创建示例数据
        self._create_sample_data(project, request.user)
        
        serializer = self.get_serializer(project)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    
    def _create_sample_data(self, project, user):
        """创建示例数据"""
        # 用户管理集合
        user_collection = ApiCollection.objects.create(
            project=project,
            name='用户管理',
            description='用户注册、登录、信息管理相关接口',
            order=1
        )
        
        # 用户注册接口
        ApiRequest.objects.create(
            collection=user_collection,
            name='用户注册',
            description='新用户注册接口',
            method='POST',
            url='{{base_url}}/api/users/register',
            headers={'Content-Type': 'application/json'},
            body={
                'type': 'json',
                'data': {
                    'username': 'testuser',
                    'email': 'test@example.com',
                    'password': 'password123'
                }
            },
            created_by=user,
            order=1
        )
        
        # 用户登录接口
        ApiRequest.objects.create(
            collection=user_collection,
            name='用户登录',
            description='用户登录获取token',
            method='POST',
            url='{{base_url}}/api/users/login',
            headers={'Content-Type': 'application/json'},
            body={
                'type': 'json',
                'data': {
                    'username': 'testuser',
                    'password': 'password123'
                }
            },
            created_by=user,
            order=2
        )
        
        # 宠物管理集合
        pet_collection = ApiCollection.objects.create(
            project=project,
            name='宠物管理',
            description='宠物信息增删改查接口',
            order=2
        )
        
        # 获取宠物列表
        ApiRequest.objects.create(
            collection=pet_collection,
            name='获取宠物列表',
            description='分页获取宠物列表',
            method='GET',
            url='{{base_url}}/api/pets',
            headers={'Authorization': 'Bearer {{token}}'},
            params={'page': '1', 'limit': '10'},
            created_by=user,
            order=1
        )
        
        # 创建宠物
        ApiRequest.objects.create(
            collection=pet_collection,
            name='创建宠物',
            description='添加新宠物信息',
            method='POST',
            url='{{base_url}}/api/pets',
            headers={
                'Content-Type': 'application/json',
                'Authorization': 'Bearer {{token}}'
            },
            body={
                'type': 'json',
                'data': {
                    'name': '小白',
                    'category': 'dog',
                    'age': 2,
                    'price': 1000
                }
            },
            created_by=user,
            order=2
        )


class ApiCollectionViewSet(viewsets.ModelViewSet):
    queryset = ApiCollection.objects.all()
    serializer_class = ApiCollectionSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['project', 'parent']
    
    def get_queryset(self):
        user = self.request.user
        return ApiCollection.objects.filter(
            project__in=ApiProject.objects.filter(
                models.Q(owner=user) | models.Q(members=user)
            )
        ).distinct()

    def perform_create(self, serializer):
        """创建集合时记录日志"""
        instance = serializer.save()
        log_operation(
            operation_type='create',
            resource_type='collection',
            resource_id=instance.id,
            resource_name=instance.name,
            user=self.request.user
        )

    def perform_update(self, serializer):
        """更新集合时记录日志"""
        instance = serializer.save()
        log_operation(
            operation_type='edit',
            resource_type='collection',
            resource_id=instance.id,
            resource_name=instance.name,
            user=self.request.user
        )

    def perform_destroy(self, instance):
        """删除集合时记录日志"""
        log_operation(
            operation_type='delete',
            resource_type='collection',
            resource_id=instance.id,
            resource_name=instance.name,
            user=self.request.user
        )
        instance.delete()


class ApiRequestViewSet(viewsets.ModelViewSet):
    queryset = ApiRequest.objects.all()
    serializer_class = ApiRequestSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['collection', 'method', 'request_type']
    search_fields = ['name', 'url']
    
    def get_queryset(self):
        user = self.request.user
        # 获取用户有权限的项目
        accessible_projects = ApiProject.objects.filter(
            models.Q(owner=user) | models.Q(members=user)
        )

        # 查询两种接口：
        # 1. 关联到集合的接口（集合所属项目是用户有权限的）
        # 2. 或者是用户创建的且没有关联集合的接口
        queryset = ApiRequest.objects.filter(
            models.Q(
                collection__project__in=accessible_projects
            ) | models.Q(
                collection__isnull=True,
                created_by=user
            )
        ).distinct()

        project_id = self.request.query_params.get('project')
        if project_id:
            # 如果指定了项目，则只查询该项目下的接口（包括未关联集合但创建者是当前用户的）
            queryset = queryset.filter(
                models.Q(collection__project_id=project_id) | models.Q(
                    collection__isnull=True,
                    created_by=user
                )
            ).distinct()

        return queryset

    def perform_create(self, serializer):
        """创建接口时记录日志"""
        instance = serializer.save()
        log_operation(
            operation_type='create',
            resource_type='request',
            resource_id=instance.id,
            resource_name=instance.name,
            user=self.request.user
        )

    def perform_update(self, serializer):
        """更新接口时记录日志"""
        instance = serializer.save()
        log_operation(
            operation_type='edit',
            resource_type='request',
            resource_id=instance.id,
            resource_name=instance.name,
            user=self.request.user
        )

    def perform_destroy(self, instance):
        """删除接口时记录日志"""
        log_operation(
            operation_type='delete',
            resource_type='request',
            resource_id=instance.id,
            resource_name=instance.name,
            user=self.request.user
        )
        instance.delete()
    
    @action(detail=True, methods=['post'])
    def execute(self, request, pk=None):
        """执行API请求"""
        api_request = self.get_object()
        environment_id = request.data.get('environment_id')
        
        try:
            # 创建变量解析器
            resolver = VariableResolver()

            # 解析环境变量
            variables = {}
            if environment_id:
                env = Environment.objects.get(id=environment_id)
                variables.update(env.variables)
            
            # 使用前端发送的更新后的数据，如果没有则使用数据库中的数据
            request_params = request.data.get('params', api_request.params)
            request_headers = request.data.get('headers', api_request.headers)
            request_body = request.data.get('body', api_request.body)
            request_method = request.data.get('method', api_request.method)
            request_url = request.data.get('url', api_request.url)

            # 替换URL中的变量（先解析动态函数，再替换环境变量）
            url = self._replace_variables(request_url or '', variables)
            url = resolver.resolve(url)
            
            # 准备请求头
            headers = {}
            if isinstance(request_headers, list):
                for header_item in request_headers:
                    if header_item.get('enabled', True) and header_item.get('key'):
                        key = header_item['key']
                        value = self._replace_variables(str(header_item.get('value', '')), variables)
                        value = resolver.resolve(value)
                        headers[key] = value
            else:
                headers = request_headers.copy() if request_headers else {}
                for key, value in headers.items():
                    headers[key] = self._replace_variables(str(value), variables)
                    headers[key] = resolver.resolve(headers[key])

            # 准备请求参数
            params = request_params.copy() if request_params else {}
            for key, value in params.items():
                params[key] = self._replace_variables(str(value), variables)
                params[key] = resolver.resolve(params[key])

            # 准备请求体
            body_data = None
            body_type = 'none'
            if request_body and request_method in ['POST', 'PUT', 'PATCH']:
                body_type = request_body.get('type', 'none')
                body_content = request_body.get('data')

                if body_type == 'json':
                    if isinstance(body_content, dict):
                        body_data = self._replace_variables_in_dict(body_content, variables)
                        body_data = self._resolve_variables_in_dict(body_data, resolver)
                    else:
                        body_data = body_content
                elif body_type == 'raw':
                    if isinstance(body_content, str):
                        body_data = self._replace_variables(body_content, variables)
                        body_data = resolver.resolve(body_data)
                    else:
                        body_data = body_content
                elif body_type in ['form-data', 'x-www-form-urlencoded']:
                    if isinstance(body_content, list):
                        body_data = self._replace_variables_in_dict(body_content, variables)
                        body_data = self._resolve_variables_in_dict(body_data, resolver)
                    else:
                        body_data = body_content
                else:
                    body_data = body_content
            
            # M15：注入认证信息（ApiRequest.auth 此前定义却从未附加到出站请求）
            headers, params, request_auth = apply_auth(
                headers, params, request.data.get('auth', api_request.auth)
            )

            # 执行请求
            start_time = time.time()

            # 根据请求体类型决定使用 data 还是 json 参数
            if body_type == 'raw':
                # raw 类型使用 data 参数，发送原始字符串
                response = safe_request(
                    request_method,
                    url,
                    headers=headers,
                    params=params,
                    data=body_data,
                    auth=request_auth,
                    stream=True,  # M15：按块读取，配合 read_response_body 限制落库体积
                    timeout=30
                )
            else:
                # json 类型使用 json 参数，自动序列化
                response = safe_request(
                    request_method,
                    url,
                    headers=headers,
                    params=params,
                    json=body_data,
                    auth=request_auth,
                    stream=True,
                    timeout=30
                )
            end_time = time.time()

            response_time = (end_time - start_time) * 1000  # 转换为毫秒

            # M15：受控读取响应体（超限截断并标记 truncated）
            response_text, body_truncated = read_response_body(response)


            # 执行断言验证
            assertions = request.data.get('assertions', api_request.assertions) or []
            for assertion in assertions:
                if assertion.get('type') == 'response_time':
                    assertion['actual_time'] = response_time
            assertions_results = execute_assertions(response, assertions)
            
            # 保存请求历史
            history = RequestHistory.objects.create(
                request=api_request,
                environment_id=environment_id,
                request_data={
                    'url': url,
                    'method': request_method,
                    'headers': headers,
                    'params': params,
                    'body': body_data
                },
                response_data={
                    'headers': dict(response.headers),
                    'body': response_text,
                    'json': read_response_json(response_text, response.headers),
                    'truncated': body_truncated,
                },
                status_code=response.status_code,
                response_time=response_time,
                executed_by=request.user
            )

            # 记录执行操作
            log_operation(
                operation_type='execute',
                resource_type='request',
                resource_id=api_request.id,
                resource_name=api_request.name,
                user=request.user
            )
            
            # 返回包含断言结果的数据
            history_data = RequestHistorySerializer(history).data
            history_data['assertions_results'] = assertions_results
            
            return Response(history_data)
            
        except Exception as e:
            # 保存错误历史
            history = RequestHistory.objects.create(
                request=api_request,
                environment_id=environment_id,
                request_data={
                    'url': api_request.url,
                    'method': api_request.method,
                    'headers': api_request.headers,
                    'params': api_request.params,
                    'body': api_request.body
                },
                error_message=str(e),
                executed_by=request.user
            )
            
            return Response(RequestHistorySerializer(history).data, status=status.HTTP_400_BAD_REQUEST)
    
    def _replace_variables(self, text, variables):
        """替换文本中的变量"""
        if not isinstance(text, str):
            return text
        
        result = text
        for key, value in (variables or {}).items():
            if isinstance(value, dict):
                replacement = str(value.get('currentValue', '') or value.get('initialValue', ''))
            else:
                replacement = str(value) if value is not None else ''
            result = result.replace(f'{{{{{key}}}}}', replacement)
        return result
    
    def _replace_variables_in_dict(self, data, variables):
        """递归替换字典中的变量"""
        if isinstance(data, dict):
            return {k: self._replace_variables_in_dict(v, variables) for k, v in data.items()}
        elif isinstance(data, list):
            return [self._replace_variables_in_dict(item, variables) for item in data]
        elif isinstance(data, str):
            return self._replace_variables(data, variables)
        else:
            return data

    def _resolve_variables_in_dict(self, data, resolver):
        """递归解析字典中的动态函数占位符"""
        if isinstance(data, dict):
            return {k: self._resolve_variables_in_dict(v, resolver) for k, v in data.items()}
        elif isinstance(data, list):
            return [self._resolve_variables_in_dict(item, resolver) for item in data]
        elif isinstance(data, str):
            return resolver.resolve(data)
        else:
            return data

    @action(detail=True, methods=['post'], url_path='generate-cases')
    def generate_cases(self, request, pk=None):
        """P0-2：单接口用例生成 Agent。

        调用 LLM 为该接口生成 正常/边界/异常 三类草稿用例，落 ``GeneratedApiTestCase(draft)``，
        不直接作为正式用例执行，需前端确认（adopt）后才采纳。
        """
        api_request = get_object_or_404(ApiRequest, id=pk)
        config_id = request.data.get('config_id')
        try:
            cases = ApiTestCaseAgent.generate_cases_for_request(
                api_request, config_id=config_id, user=request.user,
            )
        except SsrfBlocked as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except RuntimeError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            # LLM 推理超时/网络抖动：返回可重试的客户端错误，而非 500
            return Response({'error': f'LLM 推理超时，请稍后重试: {e}'},
                            status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.exception('单接口用例生成失败')
            return Response({'error': f'生成失败: {e}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        serializer = GeneratedApiTestCaseSerializer(cases, many=True)
        return Response({'count': len(cases), 'cases': serializer.data},
                        status=status.HTTP_201_CREATED)


class EnvironmentViewSet(viewsets.ModelViewSet):
    queryset = Environment.objects.all()
    serializer_class = EnvironmentSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['scope', 'project', 'is_active']
    ordering = ['-created_at']
    
    def get_queryset(self):
        user = self.request.user
        return Environment.objects.filter(
            models.Q(scope='GLOBAL') | 
            models.Q(
                scope='LOCAL',
                project__in=ApiProject.objects.filter(
                    models.Q(owner=user) | models.Q(members=user)
                )
            )
        ).distinct().order_by('-created_at')
    
    @action(detail=True, methods=['post'])
    def activate(self, request, pk=None):
        """激活环境"""
        environment = self.get_object()
        
        # 如果是局部环境，取消同项目下其他环境的激活状态
        if environment.scope == 'LOCAL' and environment.project:
            Environment.objects.filter(
                project=environment.project,
                scope='LOCAL'
            ).update(is_active=False)
        # 如果是全局环境，取消其他全局环境的激活状态
        elif environment.scope == 'GLOBAL':
            Environment.objects.filter(scope='GLOBAL').update(is_active=False)
        
        environment.is_active = True
        environment.save()
        
        return Response({'message': '环境已激活'})

    def perform_create(self, serializer):
        """创建环境时记录日志"""
        instance = serializer.save()
        log_operation(
            operation_type='create',
            resource_type='environment',
            resource_id=instance.id,
            resource_name=instance.name,
            user=self.request.user
        )

    def perform_update(self, serializer):
        """更新环境时记录日志"""
        instance = serializer.save()
        log_operation(
            operation_type='edit',
            resource_type='environment',
            resource_id=instance.id,
            resource_name=instance.name,
            user=self.request.user
        )

    def perform_destroy(self, instance):
        """删除环境时记录日志"""
        log_operation(
            operation_type='delete',
            resource_type='environment',
            resource_id=instance.id,
            resource_name=instance.name,
            user=self.request.user
        )
        instance.delete()


class RequestHistoryViewSet(viewsets.ModelViewSet):
    queryset = RequestHistory.objects.all()
    serializer_class = RequestHistorySerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['request__request_type', 'status_code']
    ordering = ['-executed_at']
    pagination_class = StandardPagination
    
    def get_queryset(self):
        user = self.request.user
        return RequestHistory.objects.filter(
            request__collection__project__in=ApiProject.objects.filter(
                models.Q(owner=user) | models.Q(members=user)
            )
        ).select_related(
            'request', 'environment', 'executed_by',
            'request__created_by', 'environment__created_by', 'environment__project'
        ).distinct()

    @action(detail=False, methods=['post'], url_path='batch-delete')
    def batch_delete(self, request):
        """批量删除请求历史"""
        ids = request.data.get('ids', [])
        if not ids:
            return Response({'error': '未提供要删除的记录ID'}, status=status.HTTP_400_BAD_REQUEST)
        
        # 确保只能删除有权限的记录
        # 先获取有权限的ID列表，避免在distinct()后调用delete()
        queryset = self.get_queryset()
        valid_ids = list(queryset.filter(id__in=ids).values_list('id', flat=True))
        
        # 使用有权限的ID列表进行删除
        deleted_count, _ = RequestHistory.objects.filter(id__in=valid_ids).delete()
        
        return Response({'message': f'成功删除 {deleted_count} 条记录'})


class TestSuiteViewSet(viewsets.ModelViewSet):
    queryset = TestSuite.objects.all()
    serializer_class = TestSuiteSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['project']
    
    def get_queryset(self):
        user = self.request.user
        return TestSuite.objects.filter(
            project__in=ApiProject.objects.filter(
                models.Q(owner=user) | models.Q(members=user)
            )
        ).distinct()

    @action(detail=True, methods=['post'])
    def execute(self, request, pk=None):
        """执行测试套件"""
        test_suite = self.get_object()
        
        try:
            # 创建执行记录
            execution = TestExecution.objects.create(
                test_suite=test_suite,
                status='RUNNING',
                start_time=timezone.now(),
                executed_by=request.user
            )
            
            # 获取套件中的请求
            suite_requests = TestSuiteRequest.objects.filter(
                test_suite=test_suite,
                enabled=True
            ).order_by('order')
            
            execution.total_requests = suite_requests.count()
            execution.save()
            
            results = []
            executed_items = []  # G4：收集 (ApiRequest, passed) 供安全裁判钩子使用
            passed_count = 0
            failed_count = 0
            
            # 创建变量解析器
            resolver = VariableResolver()

            # 执行每个请求
            for suite_request in suite_requests:
                api_request = suite_request.request
                
                try:
                    # 解析环境变量
                    variables = {}
                    if test_suite.environment:
                        variables.update(test_suite.environment.variables)
                    
                    # 替换URL中的变量（先解析动态函数，再替换环境变量）
                    url = self._replace_variables(api_request.url, variables)
                    url = resolver.resolve(url)

                    # 准备请求头
                    headers = {}
                    # 支持新的数组格式和旧的对象格式
                    if isinstance(api_request.headers, list):
                        # 新的数组格式 [{"key": "Authorization", "value": "Bearer {{token}}", "enabled": true, "description": "..."}]
                        for header_item in api_request.headers:
                            if header_item.get('enabled', True) and header_item.get('key'):
                                key = header_item['key']
                                value = self._replace_variables(str(header_item.get('value', '')), variables)
                                value = resolver.resolve(value)
                                headers[key] = value
                    else:
                        # 旧的对象格式 {"Authorization": "Bearer {{token}}"}
                        headers = api_request.headers.copy()
                        for key, value in headers.items():
                            headers[key] = self._replace_variables(str(value), variables)
                            headers[key] = resolver.resolve(headers[key])

                    params = api_request.params.copy()
                    for key, value in params.items():
                        params[key] = self._replace_variables(str(value), variables)
                        params[key] = resolver.resolve(params[key])

                    body_data = None
                    if (
                        api_request.body
                        and api_request.method in ['POST', 'PUT', 'PATCH']
                        and api_request.body.get('type') == 'json'
                    ):
                        body_data = api_request.body.get('data', {})
                        body_data = self._replace_variables_in_dict(body_data, variables)
                        body_data = self._resolve_variables_in_dict(body_data, resolver)

                    # M15：注入认证信息（auth 字段此前从未生效）
                    headers, params, request_auth = apply_auth(
                        headers, params, api_request.auth
                    )

                    # 执行请求
                    start_time = time.time()
                    response = safe_request(
                        api_request.method,
                        url,
                        headers=headers,
                        params=params,
                        json=body_data,
                        auth=request_auth,
                        stream=True,  # M15：受控读取
                        timeout=30
                    )
                    end_time = time.time()
                    response_time = (end_time - start_time) * 1000

                    # M15：受控读取响应体（超限截断并标记 truncated）
                    response_text, body_truncated = read_response_body(response)


                    # 执行断言验证
                    assertions = api_request.assertions or []
                    # 添加响应时间到断言中
                    for assertion in assertions:
                        if assertion.get('type') == 'response_time':
                            assertion['actual_time'] = response_time
                    
                    # 使用共享的断言执行方法
                    assertions_results = execute_assertions(response, assertions)
                    
                    # 检查所有断言是否通过
                    passed = True
                    error_message = ''
                    
                    # 检查套件请求的断言
                    for assertion in suite_request.assertions:
                        # 简单的状态码断言
                        if assertion.get('type') == 'status_code':
                            expected = assertion.get('value')
                            if response.status_code != expected:
                                passed = False
                                error_message = f'状态码断言失败: 期望 {expected}, 实际 {response.status_code}'
                                break
                    
                    # 检查接口自身的断言
                    if passed and assertions_results:
                        for assertion_result in assertions_results:
                            if not assertion_result.get('passed', True):
                                passed = False
                                error_message = f"断言失败: {assertion_result.get('name', '未命名断言')} - {assertion_result.get('error', '断言不通过')}"
                                break
                    
                    if passed:
                        passed_count += 1
                    else:
                        failed_count += 1
                    
                    results.append({
                        'name': api_request.name,
                        'method': api_request.method,
                        'url': url,
                        'status_code': response.status_code,
                        'response_time': response_time,
                        'passed': passed,
                        'error': error_message,
                        'assertions_results': assertions_results
                    })
                    executed_items.append((api_request, passed))  # G4
                    
                    # 保存请求历史
                    RequestHistory.objects.create(
                        request=api_request,
                        environment=test_suite.environment,
                        request_data={
                            'url': url,
                            'method': api_request.method,
                            'headers': headers,
                            'params': params,
                            'body': body_data
                        },
                        response_data={
                            'headers': dict(response.headers),
                            'body': response_text,
                            'json': read_response_json(response_text, response.headers),
                            'truncated': body_truncated,
                        },
                        status_code=response.status_code,
                        response_time=response_time,
                        assertions_results=assertions_results,
                        executed_by=request.user
                    )
                    
                except Exception as e:
                    failed_count += 1
                    results.append({
                        'name': api_request.name,
                        'method': api_request.method,
                        'url': api_request.url,
                        'passed': False,
                        'error': str(e)
                    })
                    executed_items.append((api_request, False))  # G4
            
            # 更新执行结果
            execution.end_time = timezone.now()
            execution.passed_requests = passed_count
            execution.failed_requests = failed_count
            execution.status = 'COMPLETED' if failed_count == 0 else 'FAILED'
            execution.results = results
            execution.save()

            # G4：套件执行完成后，对 security 标记用例触发安全裁判（best-effort，
            # 绝不中断主执行流程；sidecar 不可达/建单缺规范项目均被钩子内部吸收）。
            try:
                from apps.testgen_integration.hooks import (
                    trigger_security_verdict_after_execution,
                )

                trigger_security_verdict_after_execution(
                    executed_items,
                    reporter=request.user,
                )
            except Exception as _g4_exc:
                logger.warning("G4 安全裁判钩子异常（忽略）: %s", _g4_exc)

            # 记录执行操作
            log_operation(
                operation_type='execute',
                resource_type='suite',
                resource_id=test_suite.id,
                resource_name=test_suite.name,
                user=request.user
            )
            
            return Response(TestExecutionSerializer(execution).data)
            
        except Exception as e:
            execution.status = 'FAILED'
            execution.end_time = timezone.now()
            execution.save()
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='adopt')
    def adopt(self, request, pk=None):
        """将 AI 生成的草稿套件转为正式套件（is_draft=False）。

        P0-2 场景 Agent 闭环：generate_scenario 产出 is_draft=True 的草稿套件，
        用户确认（采纳）后才转为正式可执行的套件。
        """
        test_suite = self.get_object()
        if not test_suite.is_draft:
            return Response({'error': '该套件不是草稿'}, status=status.HTTP_400_BAD_REQUEST)
        test_suite.is_draft = False
        test_suite.save(update_fields=['is_draft', 'updated_at'])
        serializer = TestSuiteSerializer(test_suite, context={'request': request})
        return Response(serializer.data)

    def perform_create(self, serializer):
        """创建测试套件时记录日志"""
        instance = serializer.save()
        log_operation(
            operation_type='create',
            resource_type='suite',
            resource_id=instance.id,
            resource_name=instance.name,
            user=self.request.user
        )

    def perform_update(self, serializer):
        """更新测试套件时记录日志"""
        instance = serializer.save()
        log_operation(
            operation_type='edit',
            resource_type='suite',
            resource_id=instance.id,
            resource_name=instance.name,
            user=self.request.user
        )

    def perform_destroy(self, instance):
        """删除测试套件时记录日志"""
        log_operation(
            operation_type='delete',
            resource_type='suite',
            resource_id=instance.id,
            resource_name=instance.name,
            user=self.request.user
        )
        instance.delete()

    @action(detail=True, methods=['post'], url_path='add-requests')
    def add_requests(self, request, pk=None):
        """添加请求到测试套件"""
        test_suite = self.get_object()
        request_ids = request.data.get('request_ids', [])
        
        try:
            for request_id in request_ids:
                api_request = ApiRequest.objects.get(id=request_id)
                TestSuiteRequest.objects.get_or_create(
                    test_suite=test_suite,
                    request=api_request,
                    defaults={
                        'order': TestSuiteRequest.objects.filter(test_suite=test_suite).count(),
                        'enabled': True,
                        'assertions': []
                    }
                )
            
            return Response({'message': '添加成功'})
            
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    
    def _replace_variables(self, text, variables):
        """替换文本中的变量"""
        if not isinstance(text, str):
            return text
        
        result = text
        for key, value in (variables or {}).items():
            if isinstance(value, dict):
                replacement = str(value.get('currentValue', '') or value.get('initialValue', ''))
            else:
                replacement = str(value) if value is not None else ''
            result = result.replace(f'{{{{{key}}}}}', replacement)
        return result
    
    def _replace_variables_in_dict(self, data, variables):
        """递归替换字典中的变量"""
        if isinstance(data, dict):
            return {k: self._replace_variables_in_dict(v, variables) for k, v in data.items()}
        elif isinstance(data, list):
            return [self._replace_variables_in_dict(item, variables) for item in data]
        elif isinstance(data, str):
            return self._replace_variables(data, variables)
        else:
            return data
    
    def _resolve_variables_in_dict(self, data, resolver):
        """递归解析字典中的动态函数占位符"""
        if isinstance(data, dict):
            return {k: self._resolve_variables_in_dict(v, resolver) for k, v in data.items()}
        elif isinstance(data, list):
            return [self._resolve_variables_in_dict(item, resolver) for item in data]
        elif isinstance(data, str):
            return resolver.resolve(data)
        else:
            return data

    @action(detail=False, methods=['post'], url_path='scenario/generate')
    def scenario_generate(self, request):
        """P0-2：场景用例生成 Agent。

        接收多个 ApiRequest id，AI 串联业务流，生成 ``TestSuite(is_draft=True)`` + 顺序请求草稿。
        """
        request_ids = request.data.get('request_ids') or []
        if not isinstance(request_ids, list) or not request_ids:
            return Response({'error': '请提供 request_ids 列表'},
                            status=status.HTTP_400_BAD_REQUEST)
        project_id = request.data.get('project_id')
        project = None
        if project_id:
            project = ApiProject.objects.filter(id=project_id).first()
        try:
            suite = ApiTestCaseAgent.generate_scenario(
                request_ids=request_ids, project=project,
                config_id=request.data.get('config_id'), user=request.user,
                name=request.data.get('name'),
            )
        except SsrfBlocked as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except RuntimeError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            return Response({'error': f'LLM 推理超时，请稍后重试: {e}'},
                            status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.exception('场景用例生成失败')
            return Response({'error': f'生成失败: {e}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        serializer = TestSuiteSerializer(suite, context={'request': request})
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class TestSuiteRequestViewSet(viewsets.ModelViewSet):
    queryset = TestSuiteRequest.objects.all()
    serializer_class = TestSuiteRequestSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['test_suite', 'enabled']
    
    def get_queryset(self):
        user = self.request.user
        return TestSuiteRequest.objects.filter(
            test_suite__project__in=ApiProject.objects.filter(
                models.Q(owner=user) | models.Q(members=user)
            )
        ).distinct()


class TestExecutionViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = TestExecution.objects.all()
    serializer_class = TestExecutionSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['status', 'test_suite']
    ordering = ['-created_at']
    pagination_class = StandardPagination
    
    def get_queryset(self):
        user = self.request.user
        return TestExecution.objects.filter(
            test_suite__project__in=ApiProject.objects.filter(
                models.Q(owner=user) | models.Q(members=user)
            )
        ).distinct()
    
    @action(detail=True, methods=['post'], url_path='generate-allure-report')
    def generate_allure_report(self, request, pk=None):
        """生成Allure报告数据"""
        execution = self.get_object()
        
        try:
            # 创建报告目录
            results_dir = os.path.join(settings.MEDIA_ROOT, 'api-testing', 'allure-results', f'execution_{execution.id}')
            os.makedirs(results_dir, exist_ok=True)
            
            # 生成测试结果文件
            self._generate_test_result_files(execution, results_dir)
            
            # 生成Allure报告
            report_output_dir = os.path.join(settings.MEDIA_ROOT, 'api-testing', 'allure-reports', f'execution_{execution.id}')
            os.makedirs(report_output_dir, exist_ok=True)
            
            # 使用Allure命令行工具生成完整报告
            import shutil
            import subprocess
            import time
            from pathlib import Path
            
            # 检查 Java 环境
            java_available = self._check_java_environment()
            if not java_available:
                logger.warning("Java 环境未配置，将使用简单报告")
            
            # Allure命令行工具路径 - 使用相对路径
            base_dir = Path(__file__).resolve().parent.parent.parent
            
            # 根据操作系统确定可执行文件名
            if os.name == 'nt':
                allure_executable = 'allure.bat'
            else:
                allure_executable = 'allure'
                
            allure_cmd = base_dir / 'allure' / 'bin' / allure_executable

            if not allure_cmd.exists():
                logger.warning(f"Allure command not found at: {allure_cmd}, trying system paths")
                # 尝试其他可能的路径
                possible_paths = [
                    Path('/usr/local/bin/allure'),  # 系统安装的allure
                    Path('/usr/bin/allure'),  # 系统安装的allure
                ]
                allure_cmd = None
                for path in possible_paths:
                    if path.exists():
                        allure_cmd = path
                        break
            
            # 确保所有目录存在
            os.makedirs(results_dir, exist_ok=True)
            
            if allure_cmd and java_available:
                try:
                    for _ in range(3):  # 重试机制
                        try:
                            # 如果目录已存在，先清理（处理权限问题）
                            if os.path.exists(report_output_dir):
                                try:
                                    shutil.rmtree(report_output_dir)
                                except PermissionError:
                                    logger.warning(f"无法删除目录（权限不足）：{report_output_dir}，尝试清理内容")
                                    # 尝试只删除内容，保留目录
                                    for item in os.listdir(report_output_dir):
                                        item_path = os.path.join(report_output_dir, item)
                                        try:
                                            if os.path.isdir(item_path):
                                                shutil.rmtree(item_path)
                                            else:
                                                os.remove(item_path)
                                        except Exception:  # noqa: S110  # 跳过无法删除的文件
                                            pass
                            
                            # 构建命令行参数（路径统一使用字符串格式）
                            if os.name == 'nt':
                                # Windows 下通过 cmd /c 执行批处理文件
                                cmd_list = [
                                    'cmd', '/c',
                                    str(allure_cmd),
                                    'generate',
                                    str(Path(results_dir)),
                                    '--clean',
                                    '--output', str(Path(report_output_dir))
                                ]
                            else:
                                # Linux/Mac 直接执行
                                cmd_list = [
                                    str(allure_cmd),
                                    'generate',
                                    str(Path(results_dir)),
                                    '--clean',
                                    '--output', str(Path(report_output_dir))
                                ]
                            
                            # 生成Allure报告
                            result = subprocess.run(
                                cmd_list,
                                check=True,
                                capture_output=True,
                                text=True,
                                timeout=30
                            )
                            logger.info(f"Allure 报告生成成功: {result.stdout}")
                            break
                        except subprocess.TimeoutExpired:
                            if _ == 2:  # 最后一次尝试
                                raise
                            logger.warning(f"Allure 命令超时，第 {_ + 1} 次重试...")
                            time.sleep(1)
                            continue
                except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
                    # 如果Allure命令失败，记录详细错误信息
                    error_detail = str(e)
                    if hasattr(e, 'stderr') and e.stderr:
                        error_detail = f"{error_detail}\nStderr: {e.stderr}"
                    logger.error(f"Allure 命令执行失败: {error_detail}")
                    
                    # 不再使用回退方案，直接返回错误
                    return Response({
                        'error': 'Allure 报告生成失败',
                        'detail': error_detail,
                        'suggestion': (
                            '请检查以下项目：\n'
                            '1. Java 是否已安装并配置（JAVA_HOME 或 java 命令可用）\n'
                            '2. Allure 工具是否完整（项目根目录 allure/bin/ 目录）\n'
                            '3. 目录权限是否正确\n'
                            '4. 查看后端日志获取详细错误信息'
                        )
                    }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            else:
                # 如果没有 Allure 工具或 Java 环境，生成简单的 HTML 报告
                logger.warning("Allure 工具或 Java 环境不可用，生成简单报告")
                os.makedirs(report_output_dir, exist_ok=True)
                fallback_html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>测试报告 - {execution.test_suite.name}</title>
</head>
<body>
    <h1>测试报告</h1>
    <p>测试套件: {execution.test_suite.name}</p>
    <p>状态: {execution.get_status_display()}</p>
    <p>总请求数: {execution.total_requests}</p>
    <p>通过: {execution.passed_requests}</p>
    <p>失败: {execution.failed_requests}</p>
</body>
</html>
"""
                with open(os.path.join(report_output_dir, 'index.html'), 'w', encoding='utf-8') as f:
                    f.write(fallback_html)
            
            # 创建自定义的summary.html页面作为报告概览
            status_class = "status-passed" if execution.status == "COMPLETED" else "status-failed"
            index_content = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>测试报告概览 - {execution.test_suite.name}</title>
    <style>
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            margin: 0;
            padding: 0;
            background-color: #f5f7fa;
            color: #333;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 0;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        .header-content {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            padding: 2rem;
            max-width: 1200px;
            margin: 0 auto;
            position: relative;
        }}
        .header-info {{
            flex: 1;
            text-align: center;
        }}
        .header-actions {{
            position: absolute;
            right: 2rem;
            bottom: 2rem;
        }}
        .allure-report-btn {{
            display: inline-block;
            padding: 0.8rem 1.5rem;
            background: rgba(255, 255, 255, 0.2);
            color: white;
            border: 2px solid rgba(255, 255, 255, 0.3);
            border-radius: 6px;
            text-decoration: none;
            font-weight: bold;
            transition: all 0.3s ease;
        }}
        .allure-report-btn:hover {{
            background: rgba(255, 255, 255, 0.3);
            border-color: rgba(255, 255, 255, 0.5);
            transform: translateY(-2px);
            text-decoration: none;
        }}
        .status-row {{
            display: flex;
            align-items: center;
            gap: 1rem;
            margin-bottom: 1rem;
        }}
        .execution-time {{
            color: #666;
            font-size: 0.9rem;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            padding: 2rem;
        }}
        .summary-card {{
            background: white;
            border-radius: 10px;
            padding: 2rem;
            margin-bottom: 2rem;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        .summary-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1rem;
            margin-top: 1rem;
        }}
        .summary-item {{
            text-align: center;
            padding: 1rem;
            border-radius: 8px;
        }}
        .summary-item.total {{
            background: #e3f2fd;
        }}
        .summary-item.passed {{
            background: #e8f5e9;
        }}
        .summary-item.failed {{
            background: #ffebee;
        }}
        .summary-number {{
            font-size: 2rem;
            font-weight: bold;
            display: block;
        }}
        .summary-label {{
            font-size: 0.9rem;
            opacity: 0.8;
        }}
        .status-badge {{
            display: inline-block;
            padding: 0.5rem 1rem;
            border-radius: 20px;
            font-weight: bold;
            margin-bottom: 1rem;
        }}
        .status-passed {{
            background: #4caf50;
            color: white;
        }}
        .status-failed {{
            background: #f44336;
            color: white;
        }}
        .test-results {{
            background: white;
            border-radius: 10px;
            padding: 2rem;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        .test-result-item {{
            padding: 1rem;
            border-left: 4px solid #eee;
            margin-bottom: 1rem;
            border-radius: 4px;
        }}
        .test-result-item.passed {{
            border-left-color: #4caf50;
            background: #f8fff8;
        }}
        .test-result-item.failed {{
            border-left-color: #f44336;
            background: #fff8f8;
        }}
        .test-header {{
            display: flex;
            align-items: center;
            gap: 0.5rem;
            margin-bottom: 0.5rem;
        }}
        .test-name {{
            font-weight: bold;
            font-size: 1.1rem;
        }}
        .test-method {{
            display: inline-block;
            padding: 0.2rem 0.5rem;
            border-radius: 4px;
            font-size: 0.9rem;
            margin-right: 0.5rem;
        }}
        .method-get {{ background: #2196f3; color: white; }}
        .method-post {{ background: #4caf50; color: white; }}
        .method-put {{ background: #ff9800; color: white; }}
        .method-delete {{ background: #f44336; color: white; }}
        .test-url {{
            color: #666;
            font-size: 0.9rem;
            margin: 0.5rem 0;
            word-break: break-all;
        }}
        .test-error {{
            color: #f44336;
            font-size: 0.9rem;
            margin-top: 0.5rem;
            padding: 0.5rem;
            background: #ffebee;
            border-radius: 4px;
        }}
        .footer {{
            text-align: center;
            margin-top: 2rem;
            padding: 1rem;
            color: #666;
            font-size: 0.9rem;
        }}
        a {{
            color: #667eea;
            text-decoration: none;
        }}
        a:hover {{
            text-decoration: underline;
        }}
    </style>
</head>
<body>
    <div class="header">
        <div class="header-content">
            <div class="header-info">
                <h1>接口测试报告</h1>
                <p>测试套件: {execution.test_suite.name}</p>
                <p>项目: {execution.test_suite.project.name}</p>
            </div>
            <div class="header-actions">
                <a href="index.html" target="_blank" class="allure-report-btn">查看完整Allure报告</a>
            </div>
        </div>
    </div>
    
    <div class="container">
        <div class="summary-card">
            <div class="status-row">
                <div class="status-badge {status_class}">
                    状态: {execution.get_status_display()}
                </div>
                <span class="execution-time">
                    执行时间: {execution.created_at.strftime('%Y-%m-%d %H:%M:%S') if execution.created_at else 'N/A'}
                </span>
            </div>
            
            <div class="summary-grid">
                <div class="summary-item total">
                    <span class="summary-number">{execution.total_requests or 0}</span>
                    <span class="summary-label">总请求数</span>
                </div>
                <div class="summary-item passed">
                    <span class="summary-number">{execution.passed_requests or 0}</span>
                    <span class="summary-label">通过数</span>
                </div>
                <div class="summary-item failed">
                    <span class="summary-number">{execution.failed_requests or 0}</span>
                    <span class="summary-label">失败数</span>
                </div>
            </div>
        </div>
        
        <div class="test-results">
            <h2>测试结果详情</h2>
"""
            
            # 添加测试结果列表
            if execution.results:
                for i, result in enumerate(execution.results):
                    result_class = "passed" if result.get('passed', False) else "failed"
                    method_class = f"method-{result.get('method', 'GET').lower()}"
                    index_content += f"""
            <div class="test-result-item {result_class}">
                <div class="test-header">
                    <span class="test-method {method_class}">{result.get('method', 'GET')}</span>
                    <span class="test-name">{result.get('name', f'测试请求 {i+1}')}</span>
                </div>
                <div class="test-url">{result.get('url', '')}</div>
                <div><strong>状态:</strong> {'通过' if result.get('passed', False) else '失败'}</div>
                {f'<div class="test-error"><strong>错误:</strong> {result.get("error", "")}</div>' if result.get('error') else ""}
            </div>
"""
            
            index_content += f"""
        </div>
        <div class="footer">
            <p>报告生成时间: {execution.created_at.strftime('%Y-%m-%d %H:%M:%S') if execution.created_at else 'N/A'}</p>
        </div>
    </div>
</body>
</html>
"""
            # 保存为summary.html，避免覆盖Allure生成的index.html
            summary_file = os.path.join(report_output_dir, 'summary.html')
            with open(summary_file, 'w', encoding='utf-8') as f:
                f.write(index_content)
            
            return Response({
                'message': 'Allure报告生成成功',
                'report_url': f'/media/api-testing/allure-reports/execution_{execution.id}/summary.html'
            })
        except Exception as e:
            import traceback
            error_detail = str(e)
            error_traceback = traceback.format_exc()
            logger.error(f"生成Allure报告失败: {error_detail}\n{error_traceback}")
            return Response({
                'error': error_detail,
                'detail': error_traceback
            }, status=status.HTTP_400_BAD_REQUEST)
    
    def _check_java_environment(self):
        """检查 Java 运行环境是否可用"""
        try:
            # 首先检查 JAVA_HOME 环境变量
            java_home = os.environ.get('JAVA_HOME')
            if java_home:
                logger.info(f"检测到 JAVA_HOME: {java_home}")
            
            # 尝试执行 java -version 命令
            result = subprocess.run(
                ['java', '-version'],
                check=False,
                capture_output=True,
                text=True,
                timeout=5
            )  # 不抛异常，靠 returncode 判断 Java 可用性
            
            if result.returncode == 0:
                # Java 可用，记录版本信息
                java_version = result.stderr.split('\n')[0] if result.stderr else 'Unknown'
                logger.info(f"Java 环境可用: {java_version}")
                return True
            else:
                logger.warning(f"Java 命令执行失败: {result.stderr}")
                return False
                
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.warning(f"Java 环境检查失败: {e!s}")
            return False
    
    def _generate_test_result_files(self, execution, report_dir):
        """生成测试结果文件"""
        try:
            # 检查execution.results是否存在
            if not execution.results:
                logger.warning(f"执行记录 {execution.id} 没有结果数据")
                return

            # 生成容器文件，定义测试套件
            container_data = {
                "uuid": str(execution.id),
                "name": execution.test_suite.name,
                "children": []
            }

            # 为每个测试请求添加到children列表
            for i, result in enumerate(execution.results):
                container_data["children"].append(f"{execution.id}-{i}")

            # 保存容器文件
            container_file_path = os.path.join(report_dir, f'{execution.id}-container.json')
            with open(container_file_path, 'w', encoding='utf-8') as f:
                json.dump(container_data, f, ensure_ascii=False, indent=2)

            # 只生成每个测试请求的结果文件，不生成测试套件的结果文件
            for i, result in enumerate(execution.results):
                request_result = {
                    "uuid": f"{execution.id}-{i}",
                    "name": result.get('name', f'测试请求 {i+1}'),
                    "status": "passed" if result.get('passed', False) else "failed",
                    "stage": "finished",
                    "start": int(time.time() * 1000) - 1000,  # 模拟开始时间
                    "stop": int(time.time() * 1000),  # 模拟结束时间
                    "description": f"Method: {result.get('method', 'GET')}\nURL: {result.get('url', '')}",
                    "historyId": f"{execution.test_suite.id}-{i}",
                    "fullName": f"{execution.test_suite.name} / {result.get('name', f'请求 {i+1}')}",
                    "links": [],
                    "labels": [
                        {"name": "suite", "value": execution.test_suite.name},
                        {"name": "testClass", "value": execution.test_suite.name},
                        {"name": "package", "value": "api_testing"},
                        {"name": "project", "value": execution.test_suite.project.name}
                    ],
                    "parameters": [
                        {"name": "method", "value": result.get('method', 'GET')},
                        {"name": "url", "value": result.get('url', '')}
                    ],
                    "steps": [
                        {
                            "name": "发送请求",
                            "status": "passed",
                            "stage": "finished",
                            "start": int(time.time() * 1000) - 1000,
                            "stop": int(time.time() * 1000) - 500,
                            "steps": []
                        },
                        {
                            "name": "验证响应",
                            "status": "passed" if result.get('passed', False) else "failed",
                            "stage": "finished",
                            "start": int(time.time() * 1000) - 500,
                            "stop": int(time.time() * 1000),
                            "steps": []
                        }
                    ]
                }
                
                # 添加错误信息（如果有的话）
                if result.get('error'):
                    request_result["statusDetails"] = {
                        "message": result.get('error'),
                        "trace": ""
                    }
                
                # 保存请求结果
                request_file_path = os.path.join(report_dir, f'{execution.id}-{i}-result.json')
                with open(request_file_path, 'w', encoding='utf-8') as f:
                    json.dump(request_result, f, ensure_ascii=False, indent=2)

        except Exception as e:
            import traceback
            logger.error(f"生成测试结果文件失败: {e!s}\n{traceback.format_exc()}")
            raise


    @action(detail=True, methods=['post'], url_path='attribution')
    def attribution(self, request, pk=None):
        """P0-2：失败执行 AI 归因。

        仅对状态为 FAILED 的执行记录生效；基于请求/响应/断言/日志产出根因与修复建议，
        写回 ``execution.attribution``。SSRF 守卫拦截属用户输入非法（400）。
        """
        execution = get_object_or_404(TestExecution, id=pk)
        if execution.status != 'FAILED':
            return Response({'error': '仅失败的执行记录可触发归因', 'status': execution.status},
                            status=status.HTTP_400_BAD_REQUEST)
        config_id = request.data.get('config_id')
        try:
            attribution = FailureAttributionService.attribute(
                execution, config_id=config_id,
            )
        except SsrfBlocked as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except RuntimeError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            return Response({'error': f'LLM 推理超时，请稍后重试: {e}'},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except Exception as e:
            logger.exception('失败归因失败')
            return Response({'error': f'归因失败: {e}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response({'execution_id': pk, 'attribution': attribution})

    @action(detail=True, methods=['post'], url_path='regenerate-attribution')
    def regenerate_attribution(self, request, pk=None):
        """对已归因过的执行重新归因（覆盖原 attribution）。"""
        execution = get_object_or_404(TestExecution, id=pk)
        config_id = request.data.get('config_id')
        try:
            attribution = FailureAttributionService.attribute(
                execution, config_id=config_id,
            )
        except SsrfBlocked as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except RuntimeError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            return Response({'error': f'LLM 推理超时，请稍后重试: {e}'},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except Exception as e:
            logger.exception('重新归因失败')
            return Response({'error': f'归因失败: {e}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response({'execution_id': pk, 'attribution': attribution})


class UserViewSet(viewsets.ReadOnlyModelViewSet):
    """用户列表接口，用于项目成员选择"""
    queryset = User.objects.all().order_by('username')
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter]
    search_fields = ['username', 'email', 'first_name', 'last_name']


class ScheduledTaskViewSet(viewsets.ModelViewSet):
    """定时任务视图集"""
    queryset = ScheduledTask.objects.all()
    serializer_class = ScheduledTaskSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'description']
    ordering_fields = ['created_at', 'updated_at', 'last_run_time']
    ordering = ['-created_at']
    
    def get_queryset(self):
        """根据用户权限过滤任务"""
        queryset = super().get_queryset()
        
        # 管理员可以看到所有任务
        if self.request.user.is_staff:
            return queryset
        
        # 普通用户只能看到自己创建的任务
        return queryset.filter(created_by=self.request.user)
    
    @action(detail=True, methods=['post'])
    def run_now(self, request, pk=None):
        """立即执行定时任务"""
        import logging
        logger = logging.getLogger(__name__)
        logger.info("=== run_now 方法被调用 ===")
        
        task = self.get_object()
        logger.info(f"获取任务对象: {task.id} - {task.name}")
        
        # 检查权限
        if not request.user.is_staff and task.created_by != request.user:
            logger.info("权限检查失败")
            return Response(
                {'error': '无权执行此任务'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        try:
            # 创建执行日志
            execution_log = TaskExecutionLog.objects.create(
                task=task,
                status='PENDING',
                executed_by=request.user
            )
            logger.info(f"创建执行日志: {execution_log.id}")
            
            # 异步执行任务
            logger.info("派发任务执行")
            dispatch_mode, celery_task_id = self._execute_task_async(
                task, execution_log, request.user
            )
            
            logger.info("任务已开始执行（mode=%s）", dispatch_mode)
            return Response(
                {
                    'message': '任务已开始执行',
                    'execution_id': execution_log.id,
                    'dispatch_mode': dispatch_mode,
                    'celery_task_id': celery_task_id,
                },
                status=status.HTTP_200_OK
            )

        except TaskQueueFull as e:
            # L8：兜底池已满 → 明确拒绝，且把失败原因落到执行日志（不静默丢弃）
            execution_log.status = 'FAILED'
            execution_log.end_time = timezone.now()
            execution_log.error_message = str(e)
            execution_log.save(update_fields=['status', 'end_time', 'error_message'])
            return Response(
                {'error': str(e)},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )
            
        except Exception as e:
            return Response(
                {'error': f'执行任务失败: {e!s}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['post'])
    def activate(self, request, pk=None):
        """激活定时任务"""
        task = self.get_object()
        
        if task.status == 'ACTIVE':
            return Response(
                {'error': '任务已经是激活状态'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        task.status = 'ACTIVE'
        task.next_run_time = task.calculate_next_run()
        task.save()
        
        return Response(
            {'message': '任务已激活', 'next_run_time': task.next_run_time},
            status=status.HTTP_200_OK
        )
    
    @action(detail=True, methods=['post'])
    def pause(self, request, pk=None):
        """暂停定时任务"""
        task = self.get_object()
        
        if task.status == 'PAUSED':
            return Response(
                {'error': '任务已经是暂停状态'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        task.status = 'PAUSED'
        task.next_run_time = None
        task.save()
        
        return Response(
            {'message': '任务已暂停'},
            status=status.HTTP_200_OK
        )
    
    @action(detail=True, methods=['get'])
    def execution_logs(self, request, pk=None):
        """获取任务执行日志"""
        task = self.get_object()
        
        # 检查权限
        if not request.user.is_staff and task.created_by != request.user:
            return Response(
                {'error': '无权查看此任务的执行日志'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        logs = TaskExecutionLog.objects.filter(task=task).order_by('-created_at')
        page = self.paginate_queryset(logs)
        
        if page is not None:
            serializer = TaskExecutionLogSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = TaskExecutionLogSerializer(logs, many=True)
        return Response(serializer.data)
    
    def _execute_task_async(self, task, execution_log, user=None):
        """提交一次定时任务执行（L8）：Celery 优先，broker 不可用退回有界线程池。

        返回 `(dispatch_mode, celery_task_id)`；不再「每请求一条裸线程」。
        """
        return _dispatch_scheduled_task(task, execution_log, getattr(user, 'id', None))

    def _execute_and_record(self, task, execution_log):
        """真正执行任务并回写状态（Celery worker 与兜底线程池共用同一份逻辑）。

        入参保持 ORM 对象：worker 侧由 `_run_task_sync` 按主键重新取对象，
        避免跨进程传递陈旧实例。
        """
        import logging

        logger = logging.getLogger(__name__)
        logger.info("=== 开始执行定时任务 ID=%s ===", task.id)

        def execute():
            try:
                # 更新执行状态
                execution_log.status = 'RUNNING'
                execution_log.start_time = timezone.now()
                execution_log.save()
                
                # 执行任务
                if task.task_type == 'TEST_SUITE':
                    result = self._execute_test_suite(task)
                elif task.task_type == 'API_REQUEST':
                    result = self._execute_api_request(task)
                else:
                    raise ValueError(f"未知的任务类型: {task.task_type}")
                
                # 更新执行结果
                execution_log.status = 'COMPLETED'
                execution_log.end_time = timezone.now()
                execution_log.result = result
                execution_log.save()
                
                # 更新任务统计
                task.update_run_stats(success=True)
                task.last_result = result
                task.save()
                
                logger.info("=== 开始检查发送成功通知 ===")
                # 发送通知（如果配置了）
                # 检查任务是否有通知设置
                notification_setting = None
                if hasattr(task, 'notification_settings'):
                    try:
                        notification_setting = task.notification_settings.first()
                        logger.info(f"获取到通知设置: {notification_setting}")
                        if notification_setting:
                            logger.info(f"通知设置详情 - ID: {notification_setting.id}, 是否启用: {notification_setting.is_enabled}, 成功通知: {notification_setting.notify_on_success}")
                        else:
                            logger.info("没有找到通知设置")
                    except Exception as e:
                        logger.error(f"获取任务通知设置时出错: {e}")
                        import traceback
                        traceback.print_exc()
                else:
                    logger.info("任务没有notification_settings属性")
                
                if notification_setting and notification_setting.is_enabled:
                    logger.info("通知设置已启用，准备发送成功通知")
                    if notification_setting.notify_on_success:
                        logger.info("调用 _send_notification 方法发送成功通知")
                        self._send_notification(task, execution_log, success=True)
                    else:
                        logger.info("通知设置中未启用成功通知")
                else:
                    logger.info("通知设置未启用或不存在，跳过成功通知")
                logger.info("=== 结束检查发送成功通知 ===")
                
            except Exception as e:
                # 记录执行失败
                execution_log.status = 'FAILED'
                execution_log.end_time = timezone.now()
                execution_log.error_message = str(e)
                execution_log.save()
                
                # 更新任务统计
                task.update_run_stats(success=False)
                task.error_message = str(e)
                task.save()
                
                logger.info("=== 开始检查发送失败通知 ===")
                # 发送失败通知（如果配置了）
                # 检查任务是否有通知设置
                notification_setting = None
                if hasattr(task, 'notification_settings'):
                    try:
                        notification_setting = task.notification_settings.first()
                        logger.info(f"获取到通知设置（失败情况）: {notification_setting}")
                        if notification_setting:
                            logger.info(f"通知设置详情（失败情况） - ID: {notification_setting.id}, 是否启用: {notification_setting.is_enabled}, 失败通知: {notification_setting.notify_on_failure}")
                        else:
                            logger.info("没有找到通知设置（失败情况）")
                    except Exception as e:
                        logger.error(f"获取任务通知设置时出错（失败情况）: {e}")
                        import traceback
                        traceback.print_exc()
                else:
                    logger.info("任务没有notification_settings属性（失败情况）")
                
                if notification_setting and notification_setting.is_enabled:
                    logger.info("通知设置已启用，准备发送失败通知")
                    if notification_setting.notify_on_failure:
                        logger.info("调用 _send_notification 方法发送失败通知")
                        self._send_notification(task, execution_log, success=False)
                    else:
                        logger.info("通知设置中未启用失败通知")
                else:
                    logger.info("通知设置未启用或不存在，跳过失败通知")
                logger.info("=== 结束检查发送失败通知 ===")
        
        return execute()
    
    def _run_task_sync(self, task_id, execution_log_id):
        """按主键重新取对象并执行（Celery worker 入口；连接失效时自动重连）。"""
        from django.db import close_old_connections

        close_old_connections()
        try:
            task = ScheduledTask.objects.get(id=task_id)
            execution_log = TaskExecutionLog.objects.get(id=execution_log_id)
            return self._execute_and_record(task, execution_log)
        finally:
            close_old_connections()

    def _execute_test_suite(self, task):
        """执行测试套件"""
        from .utils import execute_test_suite
        
        result = execute_test_suite(
            task.test_suite, 
            task.environment, 
            task.created_by
        )
        return result
    
    def _execute_api_request(self, task):
        """执行API请求"""
        from .utils import execute_api_request
        
        result = execute_api_request(
            task.api_request, 
            task.environment, 
            task.created_by
        )
        return result
    
    def _send_notification(self, task, execution_log, success=True):
        """发送通知邮件"""
        try:
            import logging
            logger = logging.getLogger(__name__)

            logger.info("=== _send_notification 方法被调用 ===")
            logger.info(f"任务ID: {task.id}, 任务名称: {task.name}, 执行状态: {success}")

            # 检查任务是否有通知设置
            notification_setting = None
            if hasattr(task, 'notification_settings'):
                try:
                    notification_setting = task.notification_settings.first()
                    logger.info(f"获取到通知设置: {notification_setting}")
                except Exception as e:
                    logger.error(f"获取任务通知设置时出错: {e}")
                    import traceback
                    traceback.print_exc()

            if not notification_setting:
                logger.warning(f"任务 {task.id} 没有通知设置")
                return

            logger.info(f"通知设置详情 - ID: {notification_setting.id}, 是否启用: {notification_setting.is_enabled}")

            if not notification_setting.is_enabled:
                logger.info(f"任务 {task.id} 的通知设置未启用")
                return

            # 检查是否应该发送通知
            execution_status = 'success' if success else 'failed'
            should_notify = notification_setting.should_notify(execution_status)
            logger.info(f"执行状态: {execution_status}, should_notify结果: {should_notify}")
            if not should_notify:
                logger.info(f"根据执行状态 {execution_status}，不应该发送通知")
                return

            logger.info("通过了通知条件检查")

            # 获取通知配置
            notification_config = notification_setting.get_notification_config()
            
            # 检查是否有通知配置或自定义配置
            has_config = notification_config is not None
            has_custom_bots = bool(notification_setting.custom_webhook_bots)
            has_custom_recipients = notification_setting.custom_recipients.exists()
            
            if not (has_config or has_custom_bots or has_custom_recipients):
                logger.warning("没有找到通知配置且无自定义设置")
                return

            if notification_config:
                logger.info(f"找到了通知配置: {notification_config.name}")
            else:
                logger.info("使用自定义通知设置")

            # 根据通知类型发送不同类型的通知
            logger.info(f"通知类型: {notification_setting.notification_type}")

            if notification_setting.notification_type in ['email', 'both']:
                logger.info("发送邮件通知")
                self._send_email_notification(task, execution_log, notification_setting, notification_config, success)

            if notification_setting.notification_type in ['webhook', 'both']:
                logger.info("发送Webhook通知")
                self._send_webhook_notification(task, execution_log, notification_setting, notification_config, success)

        except Exception:
            logger.exception("发送通知失败")

    def _send_email_notification(self, task, execution_log, notification_setting, notification_config, success):
        """发送邮件通知"""
        try:
            import logging
            logger = logging.getLogger(__name__)
            from django.conf import settings
            from django.core.mail import send_mail

            logger.info("=== 开始发送邮件通知 ===")

            # 准备邮件内容
            subject = f"定时任务执行{'成功' if success else '失败'}: {task.name}"

            # 过滤掉详细的测试结果数据，只保留概要信息
            summary_info = '无详细信息'
            if execution_log.result:
                result_data = execution_log.result
                # 只保留高级概要字段,过滤掉详细的'results'数组
                summary_fields = {
                    'success': result_data.get('success'),
                    'execution_id': result_data.get('execution_id'),
                    'passed_count': result_data.get('passed_count'),
                    'failed_count': result_data.get('failed_count'),
                    'total_count': result_data.get('total_count')
                }
                # 只保留有值的字段
                summary_info = '\n'.join([f'{k}: {v}' for k, v in summary_fields.items() if v is not None])

            message = f"""
            任务名称: {task.name}
            执行状态: {'成功' if success else '失败'}
            执行时间: {execution_log.created_at.strftime('%Y-%m-%d %H:%M:%S')}
            任务类型: {'测试套件执行' if task.task_type == 'TEST_SUITE' else 'API请求执行'}

            执行概要:
            {summary_info}

            错误信息:
            {execution_log.error_message if execution_log.error_message else '无错误信息'}
            """

            # 获取收件人列表
            recipients = []
            # 首先检查自定义收件人
            if notification_setting.custom_recipients.exists():
                recipients = [user.email for user in notification_setting.custom_recipients.all() if user.email]
                logger.info(f"使用自定义收件人: {recipients}")

            # 如果定时任务表单中指定了通知邮箱，也添加到收件人列表
            if hasattr(task, 'notify_emails') and task.notify_emails:
                if isinstance(task.notify_emails, list):
                    recipients.extend(task.notify_emails)
                else:
                    recipients.append(task.notify_emails)
                logger.info(f"添加任务表单中的通知邮箱: {task.notify_emails}")

            # 去重收件人
            recipients = list(set(recipients))
            logger.info(f"最终收件人列表: {recipients}")

            if not recipients:
                logger.warning("没有找到任何邮件收件人")
                return

            # 发送邮件
            from_email = settings.DEFAULT_FROM_EMAIL
            logger.info(f"准备发送邮件，发件人: {from_email}, 收件人: {recipients}")
            send_mail(
                subject=subject,
                message=message,
                from_email=from_email,
                recipient_list=recipients,
                fail_silently=False,
            )
            logger.info("邮件发送成功")

            # 记录通知日志
            from .models import NotificationLog
            NotificationLog.objects.create(
                task=task,
                task_name=task.name,
                task_type=task.task_type,
                notification_type='task_execution',
                sender_name='系统邮件通知',
                sender_email=from_email,
                recipient_info=[{'email': email} for email in recipients],
                notification_content=message,
                status='success',
                sent_at=timezone.now()
            )

        except Exception as e:
            logger.exception("发送邮件通知失败")
            # 记录通知发送失败的日志
            try:
                from .models import NotificationLog
                NotificationLog.objects.create(
                    task=task,
                    task_name=task.name,
                    task_type=task.task_type,
                    notification_type='task_execution',
                    sender_name='系统邮件通知',
                    sender_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_info=[{'email': email} for email in recipients] if 'recipients' in locals() else [],
                    notification_content=f"发送邮件通知失败: {e!s}",
                    status='failed',
                    error_message=str(e)
                )
            except Exception:  # noqa: S110  # 忽略失败日志写入异常
                pass

    def _send_webhook_notification(self, task, execution_log, notification_setting, notification_config, success):
        """发送Webhook通知"""
        try:
            import json
            import logging

            import requests
            logger = logging.getLogger(__name__)

            logger.info("=== 开始发送Webhook通知 ===")

            all_webhook_bots = []

            # 使用统一的通知配置
            try:
                from apps.core.models import UnifiedNotificationConfig
                all_webhook_configs = UnifiedNotificationConfig.objects.filter(
                    config_type__in=['webhook_wechat', 'webhook_feishu', 'webhook_dingtalk'],
                    is_active=True
                )
                logger.info("使用统一通知配置 (UnifiedNotificationConfig)")

                for config in all_webhook_configs:
                    bots = config.get_webhook_bots()
                    for bot in bots:
                        # 只添加启用了"接口测试"的机器人
                        if bot.get('enabled', True) and bot.get('enable_api_testing', True):
                            all_webhook_bots.append(bot)
                            logger.info(f"从统一配置获取机器人: {bot.get('name')} (接口测试已启用)")
                        elif bot.get('enabled', True):
                            logger.info(f"统一配置机器人 {bot.get('name')} 未启用接口测试，跳过")

            except ImportError:
                logger.warning("无法导入统一配置，尝试使用 API 测试模块配置")
                # 回退到旧的逻辑
                if notification_config:
                    bots = notification_config.get_webhook_bots()
                    for bot in bots:
                        if bot.get('enabled', True):
                            all_webhook_bots.append(bot)
                            logger.info(f"从 API 测试配置获取机器人: {bot.get('name')}")
            except Exception as e:
                logger.error(f"获取统一配置时出错: {e}")

            # 获取自定义机器人配置 (覆盖同名/同类型或者是累加，这里选择累加)
            if notification_setting.custom_webhook_bots:
                logger.info(f"发现自定义Webhook机器人配置: {len(notification_setting.custom_webhook_bots)}个")
                for bot_type, bot_config in notification_setting.custom_webhook_bots.items():
                    # 构造统一的bot结构
                    bot_data = {
                        'type': bot_type,
                        'name': bot_config.get('name', f'自定义{bot_type}机器人'),
                        'webhook_url': bot_config.get('webhook_url'),
                        'enabled': bot_config.get('enabled', True)
                    }
                    if bot_type == 'dingtalk' and bot_config.get('secret'):
                        bot_data['secret'] = bot_config.get('secret')

                    if bot_data.get('enabled', True) and bot_data.get('webhook_url'):
                        all_webhook_bots.append(bot_data)

            if not all_webhook_bots:
                logger.warning("没有找到任何启用的webhook机器人配置")
                return

            logger.info(f"总共找到 {len(all_webhook_bots)} 个待发送的webhook机器人")

            # 准备通知内容
            status_text = '成功' if success else '失败'

            # 为不同的机器人平台准备消息格式
            for bot in all_webhook_bots:
                if not bot.get('enabled', True) or not bot.get('webhook_url'):
                    logger.info(f"跳过未启用或无URL的机器人: {bot.get('name', 'Unknown')}")
                    continue

                bot_type = bot.get('type', 'unknown')
                webhook_url = bot['webhook_url']
                logger.info(f"发送通知到 {bot_type} 机器人: {bot.get('name', 'Unknown')}")

                # 根据机器人类型构造消息格式
                if bot_type == 'wechat':  # 企业微信
                    message_data = {
                        "msgtype": "markdown",
                        "markdown": {
                            "content": f"""**定时任务执行{status_text}**

任务名称: {task.name}

执行状态: {status_text}

执行时间: {execution_log.created_at.strftime('%Y-%m-%d %H:%M:%S')}

任务类型: {'测试套件执行' if task.task_type == 'TEST_SUITE' else 'API请求执行'}"""
                        }
                    }
                elif bot_type == 'feishu':  # 飞书
                    message_data = {
                        "msg_type": "interactive",
                        "card": {
                            "elements": [{
                                "tag": "div",
                                "text": {
                                    "content": f"**定时任务执行{status_text}**\n任务名称: {task.name}\n执行状态: {status_text}\n执行时间: {execution_log.created_at.strftime('%Y-%m-%d %H:%M:%S')}\n任务类型: {'测试套件执行' if task.task_type == 'TEST_SUITE' else 'API请求执行'}",
                                    "tag": "lark_md"
                                }
                            }],
                            "header": {
                                "title": {
                                    "content": f"定时任务执行{status_text}",
                                    "tag": "plain_text"
                                },
                                "template": "green" if success else "red"
                            }
                        }
                    }
                elif bot_type == 'dingtalk':  # 钉钉
                    message_data = {
                        "msgtype": "markdown",
                        "markdown": {
                            "title": f"定时任务执行{status_text}",
                            "text": f"""**定时任务执行{status_text}**

任务名称: {task.name}

执行状态: {status_text}

执行时间: {execution_log.created_at.strftime('%Y-%m-%d %H:%M:%S')}

任务类型: {'测试套件执行' if task.task_type == 'TEST_SUITE' else 'API请求执行'}"""
                        }
                    }

                    # 钉钉机器人签名验证
                    secret = bot.get('secret')
                    if secret:
                        import base64
                        import hashlib
                        import hmac
                        import time
                        import urllib.parse

                        timestamp = str(round(time.time() * 1000))
                        string_to_sign = f'{timestamp}\n{secret}'
                        string_to_sign_enc = string_to_sign.encode('utf-8')
                        secret_enc = secret.encode('utf-8')
                        hmac_code = hmac.new(secret_enc, string_to_sign_enc, digestmod=hashlib.sha256).digest()
                        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))

                        # 在URL中添加签名参数
                        if '?' in webhook_url:
                            webhook_url += f'&timestamp={timestamp}&sign={sign}'
                        else:
                            webhook_url += f'?timestamp={timestamp}&sign={sign}'

                        # M16：不再打印 string_to_sign / sign / 带令牌的完整 URL
                        logger.info(
                            "钉钉机器人签名已生成（时间戳 %s，目标 %s）",
                            timestamp, mask_webhook_url(webhook_url),
                        )
                    else:
                        logger.info("钉钉机器人未配置签名密钥，使用无签名模式")
                else:  # 通用格式
                    message_data = {
                        "text": f"定时任务执行{status_text}\n任务名称: {task.name}\n执行状态: {status_text}\n执行时间: {execution_log.created_at.strftime('%Y-%m-%d %H:%M:%S')}\n任务类型: {'测试套件执行' if task.task_type == 'TEST_SUITE' else 'API请求执行'}"
                    }

                # 发送webhook请求（M16：webhook 目标同样受 SSRF 校验约束）
                try:
                    response = safe_request(
                        'POST',
                        webhook_url,
                        json=message_data,
                        headers={'Content-Type': 'application/json'},
                        timeout=10
                    )
                    response.raise_for_status()
                    logger.info(f"Webhook通知发送成功 - {bot_type}: {response.status_code}")

                    # 记录成功的通知日志
                    from .models import NotificationLog
                    NotificationLog.objects.create(
                        task=task,
                        task_name=task.name,
                        task_type=task.task_type,
                        notification_type='task_execution',
                        sender_name=f'系统Webhook通知-{bot_type}',
                        sender_email='',
                        recipient_info=[],
                        webhook_bot_info={
                            'bot_type': bot_type,
                            'bot_name': bot.get('name', 'Unknown'),
                            'webhook_url': mask_webhook_url(webhook_url)
                        },
                        notification_content=json.dumps(message_data, ensure_ascii=False),
                        status='success',
                        sent_at=timezone.now(),
                        response_info={
                            'status_code': response.status_code,
                            'response_text': response.text[:500]
                        }
                    )

                except SsrfBlocked as e:
                    logger.error(f"Webhook 目标未通过 SSRF 校验，已跳过 - {bot_type}: {e}")
                except requests.exceptions.RequestException as e:
                    logger.error(f"Webhook通知发送失败 - {bot_type}: {e!s}")

                    # 记录失败的通知日志
                    try:
                        from .models import NotificationLog
                        NotificationLog.objects.create(
                            task=task,
                            task_name=task.name,
                            task_type=task.task_type,
                            notification_type='task_execution',
                            sender_name=f'系统Webhook通知-{bot_type}',
                            sender_email='',
                            recipient_info=[],
                            webhook_bot_info={
                                'bot_type': bot_type,
                                'bot_name': bot.get('name', 'Unknown'),
                                'webhook_url': mask_webhook_url(webhook_url)
                            },
                            notification_content=json.dumps(message_data, ensure_ascii=False),
                            status='failed',
                            error_message=str(e),
                            sent_at=timezone.now()
                        )
                    except Exception:  # noqa: S110  # 忽略失败日志写入异常
                        pass

            logger.info("=== 结束发送Webhook通知 ===")

        except Exception:
            logger.exception("发送Webhook通知失败")


class TaskExecutionLogViewSet(viewsets.ReadOnlyModelViewSet):
    """任务执行日志视图集"""
    queryset = TaskExecutionLog.objects.all()
    serializer_class = TaskExecutionLogSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['task', 'status']
    ordering = ['-created_at']
    
    def get_queryset(self):
        user = self.request.user
        return TaskExecutionLog.objects.filter(
            task__created_by=user
        ).select_related('task', 'executed_by')




# ================ 通知管理相关视图集 ================


class NotificationLogViewSet(viewsets.ReadOnlyModelViewSet):
    """通知日志视图集"""
    queryset = NotificationLog.objects.all()
    serializer_class = NotificationLogSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['status', 'notification_type']
    ordering = ['-created_at']
    
    def get_queryset(self):
        user = self.request.user
        # 修复查询逻辑：通过任务的创建者或相关项目过滤通知日志
        return NotificationLog.objects.filter(
            models.Q(
                task__test_suite__project__in=ApiProject.objects.filter(
                    models.Q(owner=user) | models.Q(members=user)
                )
            ) | models.Q(
                task__api_request__collection__project__in=ApiProject.objects.filter(
                    models.Q(owner=user) | models.Q(members=user)
                )
            ) | models.Q(
                task__created_by=user
            )
        ).distinct()
    
    @action(detail=True, methods=['get'], url_path='detail')
    def get_notification_detail(self, request, pk=None):
        """获取通知详情"""
        notification = self.get_object()
        serializer = NotificationLogDetailSerializer(notification)
        return Response(serializer.data)


class TaskNotificationSettingViewSet(viewsets.ModelViewSet):
    """定时任务通知设置视图集"""
    queryset = TaskNotificationSetting.objects.all()
    serializer_class = TaskNotificationSettingSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['task', 'is_enabled']
    ordering = ['-created_at']
    
    def get_queryset(self):
        user = self.request.user
        return TaskNotificationSetting.objects.filter(
            models.Q(
                task__test_suite__project__in=ApiProject.objects.filter(
                    models.Q(owner=user) | models.Q(members=user)
                )
            ) | models.Q(
                task__api_request__collection__project__in=ApiProject.objects.filter(
                    models.Q(owner=user) | models.Q(members=user)
                )
            ) | models.Q(
                task__created_by=user
            )
        ).distinct()
    
    @action(detail=True, methods=['post'], url_path='update-settings')
    def update_notification_settings(self, request, pk=None):
        """更新通知设置"""
        setting = self.get_object()
        serializer = TaskNotificationSettingDetailSerializer(setting, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        
        return Response(serializer.data)


# ================ 通知管理相关模型 ================




class OperationLogViewSet(viewsets.ReadOnlyModelViewSet):
    """操作日志视图集"""
    queryset = OperationLog.objects.all()
    serializer_class = OperationLogSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['operation_type', 'resource_type', 'user']
    ordering = ['-created_at']
    
    def get_queryset(self):
        """返回所有操作日志（注：当前未做用户级隔离，权限隔离待评估，见任务 #276）"""
        return OperationLog.objects.all().order_by('-created_at')


class ApiDashboardViewSet(viewsets.ViewSet):
    """API测试仪表盘视图集"""
    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=['get'])
    def stats(self, request):
        """获取仪表盘统计数据"""
        user = request.user
        
        # 获取用户可访问的项目ID列表
        accessible_projects = ApiProject.objects.filter(
            models.Q(owner=user) | models.Q(members=user)
        ).distinct()
        project_ids = accessible_projects.values_list('id', flat=True)

        # 统计数据
        project_count = accessible_projects.count()
        
        # 接口数量 (通过项目关联)
        interface_count = ApiRequest.objects.filter(
            collection__project_id__in=project_ids
        ).count()
        
        # 测试套件数量
        suite_count = TestSuite.objects.filter(
            project_id__in=project_ids
        ).count()
        
        # 执行记录数量 (仅统计当前用户有权访问的)
        history_count = RequestHistory.objects.filter(
            request__collection__project_id__in=project_ids
        ).count()

        return Response({
            'project_count': project_count,
            'interface_count': interface_count,
            'suite_count': suite_count,
            'history_count': history_count
        })


class AIServiceConfigViewSet(viewsets.ModelViewSet):
    """AI服务配置视图集"""
    queryset = AIServiceConfig.objects.all()
    serializer_class = AIServiceConfigSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['service_type', 'role', 'is_active']
    search_fields = ['name', 'model_name']
    ordering_fields = ['created_at', 'name']
    ordering = ['-created_at']

    def get_queryset(self):
        user = self.request.user
        return AIServiceConfig.objects.filter(created_by=user)

    @action(detail=False, methods=['post'])
    def test_connection(self, request):
        """测试AI服务连接"""
        config_id = request.data.get('config_id')
        if not config_id:
            return Response({'error': '请提供配置ID'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            config = AIServiceConfig.objects.get(id=config_id, created_by=request.user)
        except AIServiceConfig.DoesNotExist:
            return Response({'error': '配置不存在'}, status=status.HTTP_404_NOT_FOUND)

        try:
            headers = {
                'Authorization': f'Bearer {config.get_api_key()}',  # H5：解密后使用
                'Content-Type': 'application/json'
            }

            test_data = {
                'model': config.model_name,
                'messages': [{'role': 'user', 'content': 'Hello'}],
                'max_tokens': 10
            }

            response = safe_request(
                'POST',
                f"{config.base_url}/chat/completions",
                headers=headers,
                json=test_data,
                timeout=10
            )

            if response.status_code == 200:
                return Response({'message': '连接测试成功', 'status': 'success'})
            else:
                return Response({
                    'error': f'连接测试失败: {response.status_code}',
                    'details': (response.text or '')[:2000]
                }, status=status.HTTP_400_BAD_REQUEST)

        except SsrfBlocked as e:
            # H4：目标被 SSRF 守卫拦截属「用户输入非法」，返回 400 而非 500
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except requests.exceptions.Timeout:
            return Response({'error': '连接超时'}, status=status.HTTP_408_REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as e:
            return Response({'error': f'连接失败: {e!s}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as e:
            return Response({'error': f'未知错误: {e!s}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'])
    def complete_parameter_descriptions(self, request):
        """使用AI自动补全参数描述"""
        request_id = request.data.get('request_id')
        if not request_id:
            return Response({'error': '请提供请求ID'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            api_request = ApiRequest.objects.get(id=request_id)
        except ApiRequest.DoesNotExist:
            return Response({'error': '请求不存在'}, status=status.HTTP_404_NOT_FOUND)

        try:
            config = AIServiceConfig.objects.filter(
                role='description',
                is_active=True
            ).first()

            if not config:
                return Response({'error': '未找到可用的参数描述补全AI配置'}, status=status.HTTP_400_BAD_REQUEST)

            headers = {
                'Authorization': f'Bearer {config.get_api_key()}',  # H5：解密后使用
                'Content-Type': 'application/json'
            }

            request_info = {
                'name': api_request.name,
                'description': api_request.description,
                'method': api_request.method,
                'url': api_request.url,
                'headers': api_request.headers,
                'params': api_request.params,
                'body': api_request.body
            }

            prompt = f"""请为以下API请求的参数生成详细的描述说明：

接口名称: {request_info['name']}
接口描述: {request_info['description']}
请求方法: {request_info['method']}
请求URL: {request_info['url']}

请求头参数:
{json.dumps(request_info['headers'], ensure_ascii=False, indent=2)}

URL参数:
{json.dumps(request_info['params'], ensure_ascii=False, indent=2)}

请求体参数:
{json.dumps(request_info['body'], ensure_ascii=False, indent=2)}

请为每个参数生成详细的描述说明，包括：
1. 参数用途
2. 数据类型
3. 是否必填
4. 取值范围或示例值
5. 其他注意事项

请返回JSON格式的结果，格式如下：
{{
  "headers": {{
    "参数名": "参数描述"
  }},
  "params": {{
    "参数名": "参数描述"
  }},
  "body": {{
    "参数名": "参数描述"
  }}
}}"""

            ai_data = {
                'model': config.model_name,
                'messages': [{'role': 'user', 'content': prompt}],
                'max_tokens': config.max_tokens,
                'temperature': config.temperature
            }

            response = safe_request(
                'POST',
                f"{config.base_url}/chat/completions",
                headers=headers,
                json=ai_data,
                timeout=30
            )

            if response.status_code == 200:
                result = response.json()
                content = result['choices'][0]['message']['content']
                try:
                    descriptions = json.loads(content)
                    return Response({'descriptions': descriptions})
                except json.JSONDecodeError:
                    return Response({'descriptions': {}, 'raw_content': content})
            else:
                return Response({
                    'error': f'AI服务调用失败: {response.status_code}',
                    'details': (response.text or '')[:2000]
                }, status=status.HTTP_400_BAD_REQUEST)

        except requests.exceptions.Timeout:
            return Response({'error': 'AI服务调用超时'}, status=status.HTTP_408_REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as e:
            return Response({'error': f'AI服务调用失败: {e!s}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as e:
            return Response({'error': f'未知错误: {e!s}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'])
    def generate_mock_data(self, request):
        """使用AI生成模拟数据"""
        schema = request.data.get('schema', {})
        count = request.data.get('count', 1)
        if not schema:
            return Response({'error': '请提供数据结构定义'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            config = AIServiceConfig.objects.filter(
                role='mock_data',
                is_active=True
            ).first()

            if not config:
                return Response({'error': '未找到可用的模拟数据生成AI配置'}, status=status.HTTP_400_BAD_REQUEST)

            headers = {
                'Authorization': f'Bearer {config.get_api_key()}',  # H5：解密后使用
                'Content-Type': 'application/json'
            }

            prompt = f"""请根据以下数据结构定义，生成{count}条符合该结构的模拟数据：

数据结构定义：
{json.dumps(schema, ensure_ascii=False, indent=2)}

要求：
1. 数据必须符合给定的结构定义
2. 字符串字段生成有意义的中文内容
3. 数值字段生成合理的数值
4. 日期字段生成有效的日期时间
5. 布尔字段随机生成true/false
6. 数组字段生成适当数量的元素

请返回JSON数组格式的结果。"""

            ai_data = {
                'model': config.model_name,
                'messages': [{'role': 'user', 'content': prompt}],
                'max_tokens': config.max_tokens,
                'temperature': config.temperature
            }

            response = safe_request(
                'POST',
                f"{config.base_url}/chat/completions",
                headers=headers,
                json=ai_data,
                timeout=30
            )

            if response.status_code == 200:
                result = response.json()
                content = result['choices'][0]['message']['content']
                try:
                    mock_data = json.loads(content)
                    return Response({'data': mock_data})
                except json.JSONDecodeError:
                    return Response({'data': [], 'raw_content': content})
            else:
                return Response({
                    'error': f'AI服务调用失败: {response.status_code}',
                    'details': (response.text or '')[:2000]
                }, status=status.HTTP_400_BAD_REQUEST)

        except requests.exceptions.Timeout:
            return Response({'error': 'AI服务调用超时'}, status=status.HTTP_408_REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as e:
            return Response({'error': f'AI服务调用失败: {e!s}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as e:
            return Response({'error': f'未知错误: {e!s}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'])
    def normalize_parameter_names(self, request):
        """使用AI规范化参数名称"""
        parameters = request.data.get('parameters', [])
        if not parameters:
            return Response({'error': '请提供参数列表'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            config = AIServiceConfig.objects.filter(
                role='naming',
                is_active=True
            ).first()

            if not config:
                return Response({'error': '未找到可用的参数命名规范化AI配置'}, status=status.HTTP_400_BAD_REQUEST)

            headers = {
                'Authorization': f'Bearer {config.get_api_key()}',  # H5：解密后使用
                'Content-Type': 'application/json'
            }

            params_info = '\n'.join([f"- {param.get('key', '')}: {param.get('value', '')}" for param in parameters])

            prompt = f"""请对以下API参数名称进行规范化处理，使其符合RESTful API命名规范：

{params_info}

请返回JSON格式的结果，包含：
1. 原始参数名
2. 建议的规范化参数名（使用小写字母、下划线分隔、语义清晰）
3. 修改原因

返回格式示例：
[
  {{
    "original": "userName",
    "suggested": "user_name",
    "reason": "使用下划线分隔单词，符合Python命名规范"
  }}
]"""

            ai_data = {
                'model': config.model_name,
                'messages': [{'role': 'user', 'content': prompt}],
                'max_tokens': config.max_tokens,
                'temperature': config.temperature
            }

            response = safe_request(
                'POST',
                f"{config.base_url}/chat/completions",
                headers=headers,
                json=ai_data,
                timeout=30
            )

            if response.status_code == 200:
                result = response.json()
                content = result['choices'][0]['message']['content']
                try:
                    suggestions = json.loads(content)
                    return Response({'suggestions': suggestions})
                except json.JSONDecodeError:
                    return Response({'suggestions': [], 'raw_content': content})
            else:
                return Response({
                    'error': f'AI服务调用失败: {response.status_code}',
                    'details': (response.text or '')[:2000]
                }, status=status.HTTP_400_BAD_REQUEST)

        except requests.exceptions.Timeout:
            return Response({'error': 'AI服务调用超时'}, status=status.HTTP_408_REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as e:
            return Response({'error': f'AI服务调用失败: {e!s}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as e:
            return Response({'error': f'未知错误: {e!s}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'])
    def extract_documentation(self, request):
        """使用AI提取API文档"""
        request_id = request.data.get('request_id')
        if not request_id:
            return Response({'error': '请提供请求ID'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            api_request = ApiRequest.objects.get(id=request_id)
        except ApiRequest.DoesNotExist:
            return Response({'error': '请求不存在'}, status=status.HTTP_404_NOT_FOUND)

        try:
            config = AIServiceConfig.objects.filter(
                role='doc_extractor',
                is_active=True
            ).first()

            if not config:
                return Response({'error': '未找到可用的API文档提取AI配置'}, status=status.HTTP_400_BAD_REQUEST)

            request_data = {
                'method': api_request.method,
                'url': api_request.url,
                'headers': api_request.headers,
                'params': api_request.params,
                'body': api_request.body,
                'description': api_request.description
            }

            headers = {
                'Authorization': f'Bearer {config.get_api_key()}',  # H5：解密后使用
                'Content-Type': 'application/json'
            }

            prompt = f"""请根据以下API请求信息，生成详细的API文档：

请求方法: {request_data['method']}
请求URL: {request_data['url']}
请求头: {json.dumps(request_data['headers'], ensure_ascii=False)}
URL参数: {json.dumps(request_data['params'], ensure_ascii=False)}
请求体: {json.dumps(request_data['body'], ensure_ascii=False)}
描述: {request_data['description']}

请生成包含以下内容的API文档：
1. 接口概述
2. 请求参数说明（包括路径参数、查询参数、请求头、请求体）
3. 响应示例
4. 错误码说明

请以Markdown格式返回文档内容。"""

            ai_data = {
                'model': config.model_name,
                'messages': [{'role': 'user', 'content': prompt}],
                'max_tokens': config.max_tokens,
                'temperature': config.temperature
            }

            response = safe_request(
                'POST',
                f"{config.base_url}/chat/completions",
                headers=headers,
                json=ai_data,
                timeout=30
            )

            if response.status_code == 200:
                result = response.json()
                documentation = result['choices'][0]['message']['content']
                return Response({'documentation': documentation})
            else:
                return Response({
                    'error': f'AI服务调用失败: {response.status_code}',
                    'details': (response.text or '')[:2000]
                }, status=status.HTTP_400_BAD_REQUEST)

        except requests.exceptions.Timeout:
            return Response({'error': 'AI服务调用超时'}, status=status.HTTP_408_REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as e:
            return Response({'error': f'AI服务调用失败: {e!s}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as e:
            return Response({'error': f'未知错误: {e!s}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ApiSpecImportViewSet(viewsets.ModelViewSet):
    """P0-2：OpenAPI 规格导入视图集"""

    queryset = ApiSpecImport.objects.all()
    serializer_class = ApiSpecImportSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['project', 'status', 'source']
    search_fields = ['name', 'base_url']
    ordering_fields = ['created_at', 'name']
    ordering = ['-created_at']

    def get_queryset(self):
        return ApiSpecImport.objects.filter(created_by=self.request.user)

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=False, methods=['post'], url_path='import-openapi')
    def import_openapi(self, request):
        """解析 swagger/openapi 并落库为接口资产；server URL 经 SSRF 守卫校验。"""
        raw_spec = request.data.get('raw_spec') or request.data.get('spec')
        if not raw_spec:
            return Response({'error': '请提供 raw_spec（规格内容）'},
                            status=status.HTTP_400_BAD_REQUEST)
        spec_format = (request.data.get('spec_format') or 'json').lower()
        name = request.data.get('name')
        source = request.data.get('source') or 'paste'
        project_id = request.data.get('project_id')
        project = None
        if project_id:
            project = ApiProject.objects.filter(id=project_id, owner=request.user).first()
        if not project:
            project = ApiProject.objects.filter(owner=request.user).first()
        if not project:
            return Response({'error': '未找到可用项目，请先创建 API 项目'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            spec_import = OpenApiImportService.import_openapi(
                raw_spec=raw_spec, spec_format=spec_format, project=project,
                user=request.user, name=name, source=source,
            )
        except Exception as e:
            logger.exception('OpenAPI 导入失败')
            return Response({'error': f'导入失败: {e}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        serializer = ApiSpecImportSerializer(spec_import)
        if spec_import.status == 'failed':
            return Response(serializer.data, status=status.HTTP_400_BAD_REQUEST)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class GeneratedApiTestCaseViewSet(viewsets.ModelViewSet):
    """P0-2：AI 生成的接口用例草稿视图集（确认闸门）"""

    serializer_class = GeneratedApiTestCaseSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['api_request', 'category', 'status']
    ordering_fields = ['created_at', 'category']
    ordering = ['category', 'created_at']

    def get_queryset(self):
        return GeneratedApiTestCase.objects.filter(created_by=self.request.user)

    @action(detail=True, methods=['post'], url_path='adopt')
    def adopt(self, request, pk=None):
        """确认采纳一条草稿用例（status: draft → adopted）。"""
        case = get_object_or_404(GeneratedApiTestCase, id=pk, created_by=request.user)
        if case.status != 'draft':
            return Response({'error': '该用例已处理'}, status=status.HTTP_400_BAD_REQUEST)
        case.status = 'adopted'
        case.save(update_fields=['status', 'updated_at'])
        serializer = GeneratedApiTestCaseSerializer(case)
        return Response(serializer.data)

    @action(detail=True, methods=['post'], url_path='discard')
    def discard(self, request, pk=None):
        """丢弃一条草稿用例（status: draft → discarded）。"""
        case = get_object_or_404(GeneratedApiTestCase, id=pk, created_by=request.user)
        if case.status != 'draft':
            return Response({'error': '该用例已处理'}, status=status.HTTP_400_BAD_REQUEST)
        case.status = 'discarded'
        case.save(update_fields=['status', 'updated_at'])
        serializer = GeneratedApiTestCaseSerializer(case)
        return Response(serializer.data)
