import json
import time
from django.utils import timezone
from .models import RequestHistory
from .variable_resolver import VariableResolver
from .ssrf_guard import safe_request  # H4：出站请求统一走 SSRF 校验

# M15：响应体落库上限（默认 2MB）。超限截断并标记 truncated，避免大响应撑爆内存与 DB。
DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_CHUNK_SIZE = 64 * 1024


def read_response_body(response, max_bytes=None):
    """受控读取响应体，返回 ``(text, truncated)``。

    - 请求侧应传 ``stream=True``，本函数按块读取，累计超过上限即停（内存占用 ≈ 上限 + 一块）；
    - 非 stream（响应已整体缓存）时退化为对 ``response.content`` 截断；
    - 读取结果回填 ``response._content``，使后续 ``response.text`` / ``response.json()``
      与断言函数仍可用（基于已读部分），不会抛 "content already consumed"。
    """
    limit = DEFAULT_MAX_RESPONSE_BYTES if max_bytes is None else max(0, int(max_bytes))
    truncated = False

    cached = getattr(response, "_content", False)
    if cached is not False:
        data = cached or b""
        if len(data) > limit:
            data = data[:limit]
            truncated = True
    else:
        buffer = bytearray()
        for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
            if not chunk:
                continue
            buffer.extend(chunk)
            if len(buffer) > limit:
                del buffer[limit:]
                truncated = True
                break
        data = bytes(buffer)
        if truncated:
            # 未读完即截断：主动释放连接，避免连接池被大响应长期占用
            try:
                response.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响已读数据
                pass

    response._content = data
    response._content_consumed = True
    encoding = getattr(response, "encoding", None) or "utf-8"
    try:
        text = data.decode(encoding, errors="replace")
    except (LookupError, TypeError):
        text = data.decode("utf-8", errors="replace")
    return text, truncated


def read_response_json(text, headers):
    """从已截断文本解析 JSON；非 JSON Content-Type 或解析失败返回 None。"""
    content_type = ""
    try:
        content_type = (headers.get("content-type") or "").lower()
    except AttributeError:
        content_type = ""
    if "application/json" not in content_type:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def apply_auth(headers, params, auth):
    """把 ``ApiRequest.auth`` 注入出站请求（M15：该字段此前定义却从未生效）。

    支持结构（大小写/别名兼容）::

        {"type": "none"}
        {"type": "bearer",  "token": "..."}
        {"type": "basic",   "username": "...", "password": "..."}
        {"type": "api_key", "key": "X-API-Key", "value": "...", "in": "header" | "query"}

    返回 ``(headers, params, requests_auth)``：``requests_auth`` 为 ``(user, pwd)`` 元组供
    ``requests`` 生成 Basic 头，其余类型返回 None。用户已显式设置的 Authorization 头优先，
    不被覆盖（避免“我以为设了 header 却被 auth 配置顶掉”）。
    """
    headers = dict(headers or {})
    params = dict(params or {})
    if not isinstance(auth, dict) or not auth:
        return headers, params, None

    auth_type = str(
        auth.get("type") or auth.get("authType") or auth.get("kind") or ""
    ).strip().lower()
    requests_auth = None

    if auth_type in ("bearer", "bearer_token", "token"):
        token = auth.get("token") or auth.get("access_token") or auth.get("value") or ""
        if token and not _has_header(headers, "authorization"):
            headers["Authorization"] = f"Bearer {token}"

    elif auth_type in ("basic", "basic_auth"):
        username = auth.get("username") or auth.get("user") or ""
        password = auth.get("password") or auth.get("pass") or ""
        if (username or password) and not _has_header(headers, "authorization"):
            requests_auth = (str(username), str(password))

    elif auth_type in ("api_key", "apikey", "key"):
        key = auth.get("key") or auth.get("name") or ""
        value = auth.get("value") or auth.get("api_key") or ""
        target = str(auth.get("in") or auth.get("addTo") or "header").lower()
        if key:
            if target in ("query", "params", "url"):
                params[key] = value
            else:
                headers[key] = value

    return headers, params, requests_auth


def _has_header(headers, name):
    """大小写无关地判断 header 是否已存在且非空。"""
    return any(k.lower() == name and str(v).strip() for k, v in headers.items())


def execute_assertions(response, assertions):
    """执行断言验证"""
    results = []
    
    for assertion in assertions:
        result = {
            'name': assertion.get('name', '未命名断言'),
            'type': assertion.get('type'),
            'passed': False,
            'expected': assertion.get('expected'),
            'actual': None,
            'error': None
        }
        
        try:
            assertion_type = assertion.get('type')
            expected = assertion.get('expected')
            actual = None
            passed = False
            
            if assertion_type == 'status_code':
                actual = response.status_code
                passed = actual == expected
                
            elif assertion_type == 'response_time':
                # 响应时间断言在调用方处理
                actual = assertion.get('actual_time')
                passed = actual <= expected if actual else False
                
            elif assertion_type == 'contains':
                text = response.text or ''
                pattern = str(expected)
                actual = text[:200] + '...' if len(text) > 200 else text
                passed = pattern in str(text)
                
            elif assertion_type == 'json_path':
                json_path = assertion.get('json_path', '')
                expected_value = assertion.get('expected')
                actual = None
                passed = False
                
                try:
                    # 检查响应是否为JSON格式
                    content_type = response.headers.get('content-type', '').lower()
                    if 'application/json' not in content_type:
                        raise ValueError(f"响应不是JSON格式，Content-Type: {content_type}")
                    
                    response_json = json.loads(response.text)
                    
                    # 检查JSONPath表达式是否为空
                    if not json_path:
                        raise ValueError("JSON路径表达式不能为空")
                    
                    from jsonpath_ng import parse
                    matches = parse(json_path).find(response_json)
                    actual = matches[0].value if matches else None
                    passed = str(actual) == str(expected_value)
                    
                    # 确保actual值被正确设置到result中
                    result['actual'] = actual
                except json.JSONDecodeError as e:
                    actual = None
                    passed = False
                    result['error'] = f"JSON解析失败: {str(e)}"
                    result['actual'] = actual
                except ImportError as e:
                    actual = None
                    passed = False
                    result['error'] = f"缺少依赖库: {str(e)}，请安装jsonpath-ng"
                    result['actual'] = actual
                except Exception as e:
                    actual = None
                    passed = False
                    result['error'] = f"执行错误: {str(e)}"
                    result['actual'] = actual
                    
            elif assertion_type == 'header':
                header_name = assertion.get('header_name', '')
                expected_value = assertion.get('expected_value')
                actual = response.headers.get(header_name)
                passed = actual == expected_value
                
            elif assertion_type == 'equals':
                actual = response.text.strip()
                passed = actual == str(expected).strip()
            
            # 确保在所有情况下都设置actual值
            if 'actual' not in result or result['actual'] is None:
                result['actual'] = actual
            result['passed'] = passed
            
        except Exception as e:
            result['error'] = str(e)
            result['passed'] = False
        
        results.append(result)
    
    return results


def execute_test_suite(test_suite, environment, executed_by):
    """执行测试套件并返回结果"""
    from .models import TestExecution, RequestHistory
    
    try:
        # 创建变量解析器
        resolver = VariableResolver()
        
        # 创建执行记录
        execution = TestExecution.objects.create(
            test_suite=test_suite,
            status='RUNNING',
            start_time=timezone.now(),
            executed_by=executed_by
        )
        
        # 获取套件中的请求
        suite_requests = test_suite.testsuiterequest_set.filter(enabled=True).order_by('order')
        
        execution.total_requests = suite_requests.count()
        execution.save()
        
        results = []
        passed_count = 0
        failed_count = 0
        
        # 执行每个请求
        for suite_request in suite_requests:
            api_request = suite_request.request
            
            try:
                # 解析环境变量
                variables = {}
                if environment:
                    variables.update(environment.variables)
                
                # 替换URL中的变量（先解析动态函数，再替换环境变量）
                url = _replace_variables(api_request.url, variables)
                url = resolver.resolve(url)
                
                # 准备请求头
                headers = {}
                if isinstance(api_request.headers, list):
                    for header_item in api_request.headers:
                        if header_item.get('enabled', True) and header_item.get('key'):
                            key = header_item['key']
                            value = _replace_variables(str(header_item.get('value', '')), variables)
                            value = resolver.resolve(value)
                            headers[key] = value
                else:
                    headers = api_request.headers.copy()
                    for key, value in headers.items():
                        headers[key] = _replace_variables(str(value), variables)
                        headers[key] = resolver.resolve(headers[key])
                
                # 准备请求参数
                params = api_request.params.copy() if api_request.params else {}
                for key, value in params.items():
                    params[key] = _replace_variables(str(value), variables)
                    params[key] = resolver.resolve(params[key])
                
                # 准备请求体
                body_data = None
                if api_request.body and api_request.method in ['POST', 'PUT', 'PATCH']:
                    if api_request.body.get('type') == 'json':
                        body_data = api_request.body.get('data', {})
                        body_data = _replace_variables_in_dict(body_data, variables)
                        body_data = _resolve_variables_in_dict(body_data, resolver)
                
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
                for assertion in assertions:
                    if assertion.get('type') == 'response_time':
                        assertion['actual_time'] = response_time
                
                assertions_results = execute_assertions(response, assertions)
                
                # 检查所有断言是否通过
                passed = True
                error_message = ''
                
                # 检查套件请求的断言
                for assertion in suite_request.assertions:
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
                
                # 保存请求历史
                RequestHistory.objects.create(
                    request=api_request,
                    environment=environment,
                    request_data={
                        'url': url,
                        'method': api_request.method,
                        'headers': headers,
                        '极速版params': params,
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
                    executed_by=executed_by
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
        
        # 更新执行结果
        execution.end_time = timezone.now()
        execution.passed_requests = passed_count
        execution.failed_requests = failed_count
        execution.status = 'COMPLETED' if failed_count == 0 else 'FAILED'
        execution.results = results
        execution.save()
        
        return {
            'success': True,
            'execution_id': execution.id,
            'passed_count': passed_count,
            'failed_count': failed_count,
            'total_count': execution.total_requests,
            'results': results
        }
        
    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }


def execute_api_request(api_request, environment, executed_by):
    """执行单个API请求并返回结果"""
    
    try:
        # 创建变量解析器
        resolver = VariableResolver()
        
        # 解析环境变量
        variables = {}
        if environment:
            variables.update(environment.variables)
        
        # 替换URL中的变量（先解析动态函数，再替换环境变量）
        url = _replace_variables(api_request.url, variables)
        url = resolver.resolve(url)
        
        # 准备请求头
        headers = {}
        if isinstance(api_request.headers, list):
            for header_item in api_request.headers:
                if header_item.get('enabled', True) and header_item.get('key'):
                    key = header_item['key']
                    value = _replace_variables(str(header_item.get('value', '')), variables)
                    value = resolver.resolve(value)
                    headers[key] = value
        else:
            headers = api_request.headers.copy()
            for key, value in headers.items():
                headers[key] = _replace_variables(str(value), variables)
                headers[key] = resolver.resolve(headers[key])
        
        # 准备请求参数
        params = api_request.params.copy() if api_request.params else {}
        for key, value in params.items():
            params[key] = _replace_variables(str(value), variables)
            params[key] = resolver.resolve(params[key])
        
        # 准备请求体
        body_data = None
        if api_request.body and api_request.method in ['POST', 'PUT', 'PATCH']:
            if api_request.body.get('type') == 'json':
                body_data = api_request.body.get('data', {})
                body_data = _replace_variables_in_dict(body_data, variables)
                body_data = _resolve_variables_in_dict(body_data, resolver)
        
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
        for assertion in assertions:
            if assertion.get('type') == 'response_time':
                assertion['actual_time'] = response_time
        
        assertions_results = execute_assertions(response, assertions)
        
        # 保存请求历史
        history = RequestHistory.objects.create(
            request=api_request,
            environment=environment,
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
            executed_by=executed_by
        )
        
        return {
            'success': True,
            'history_id': history.id,
            'status_code': response.status_code,
            'response_time': response_time,
            'assertions_results': assertions_results,
            'response_data': {
                'headers': dict(response.headers),
                'body': response_text,
                'json': read_response_json(response_text, response.headers),
                'truncated': body_truncated,
            }
        }
        
    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }


def _replace_variables(text, variables):
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

def _replace_variables_in_dict(data, variables):
    """递归替换字典中的变量"""
    if isinstance(data, dict):
        return {k: _replace_variables_in_dict(v, variables) for k, v in data.items()}
    elif isinstance(data, list):
        return [_replace_variables_in_dict(item, variables) for item in data]
    elif isinstance(data, str):
        return _replace_variables(data, variables)
    else:
        return data

def _resolve_variables_in_dict(data, resolver):
    """递归解析字典中的动态函数占位符"""
    if isinstance(data, dict):
        return {k: _resolve_variables_in_dict(v, resolver) for k, v in data.items()}
    elif isinstance(data, list):
        return [_resolve_variables_in_dict(item, resolver) for item in data]
    elif isinstance(data, str):
        return resolver.resolve(data)
    else:
        return data
