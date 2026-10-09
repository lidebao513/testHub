"""Django 反向代理：把 testgen 控制台及其 API 以同源方式接入主平台。

两条路由（均在 backend/urls.py 注册）：
  - ``/testgen/...``   → 转发到 sidecar 根（控制台页面）
  - ``/api/v1/...``    → 转发到 sidecar 的 ``/api/v1/...``（控制台 JS 用的绝对路径 API）

目的：让「根据项目生成测试用例」的 testgen 控制台嵌入主平台 SPA 时，
页面与 API 都走同源的 8000 端口，避免前端 iframe 直连 :8100 的跨端口问题，
也无需改动 sidecar 控制台源码（其 JS 写死了 ``/api/v1/`` 绝对路径）。

行为：
  - 转发所有方法(GET/POST/PUT/DELETE/...)与请求体；
  - 剥掉 Host 等逐跳头，其余透传(含 Authorization)；
  - query string 透传(支持 ``?embed=1&zone=``)；
  - sidecar 地址可用环境变量 ``TESTGEN_SIDECAR_URL`` 覆盖，默认 ``http://127.0.0.1:8100``。
"""

from __future__ import annotations

import os

import requests
from django.http import HttpResponse
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt

SIDECAR_BASE = os.environ.get("TESTGEN_SIDECAR_URL", "http://127.0.0.1:8100").rstrip("/")

# 不向下游透传的逐跳头/敏感头
_HOP_BY_HOP = {
    "host", "content-length", "connection", "keep-alive",
    "proxy-authenticate", "proxy-authorization", "te", "trailers",
    "transfer-encoding", "upgrade",
}


def _do_proxy(request, target_path: str) -> HttpResponse:
    target = f"{SIDECAR_BASE}/{target_path.lstrip('/')}"
    query = request.META.get("QUERY_STRING", "")
    if query:
        target += "?" + query

    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }

    body = request.body if request.method in ("POST", "PUT", "PATCH") else None

    try:
        resp = requests.request(
            method=request.method,
            url=target,
            headers=headers,
            data=body,
            cookies=request.COOKIES,
            timeout=120,
            allow_redirects=False,
            verify=False,
        )
    except requests.RequestException as exc:
        return HttpResponse(f"testgen 代理错误: 无法连接 sidecar ({target}): {exc}", status=502)

    excluded = _HOP_BY_HOP | {"content-encoding", "content-length"}
    resp_headers = {
        k: v for k, v in resp.headers.items()
        if k.lower() not in excluded
    }

    return HttpResponse(resp.content, status=resp.status_code, headers=resp_headers)


@csrf_exempt
@xframe_options_exempt
def testgen_web_proxy(request, rest: str = ""):
    """转发 /testgen/... → sidecar 根。rest 为 /testgen/ 之后的路径。"""
    return _do_proxy(request, rest)


@csrf_exempt
@xframe_options_exempt
def testgen_api_proxy(request, rest: str = ""):
    """转发 /api/v1/... → sidecar 的 /api/v1/...（控制台 JS 绝对路径 API）。"""
    return _do_proxy(request, f"api/v1/{rest}")
