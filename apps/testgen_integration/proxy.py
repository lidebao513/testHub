"""Django 反向代理：把 testgen 控制台及其 API 以同源方式接入主平台。

两条路由（均在 backend/urls.py 注册）：
  - ``/testgen/...``   → 转发到 sidecar 根（控制台页面）
  - ``/api/v1/...``    → 转发到 sidecar 的 ``/api/v1/...``（控制台 JS 用的绝对路径 API）

目的：让「根据项目生成测试用例」的 testgen 控制台嵌入主平台 SPA 时，
页面与 API 都走同源的 8900 端口，避免前端 iframe 直连 :8910 的跨端口问题，
也无需改动 sidecar 控制台源码（其 JS 写死了 ``/api/v1/`` 绝对路径）。

行为：
  - 转发所有方法(GET/POST/PUT/DELETE/...)与请求体；
  - 剥掉 Host 等逐跳头，其余透传(含 Authorization)；
  - query string 透传(支持 ``?embed=1&zone=``)；
  - sidecar 地址可用环境变量 ``TESTGEN_SIDECAR_URL`` 覆盖，默认 ``http://127.0.0.1:8910``。
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time

import requests
from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect
from django.urls import get_resolver
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger(__name__)

SIDECAR_BASE = os.environ.get("TESTGEN_SIDECAR_URL", "http://127.0.0.1:8910").rstrip(
    "/"
)

# M1：代理→sidecar 应使用服务端配置的共享令牌鉴权（H8 要求 sidecar 必须校验
# X-Auth-Token），而非依赖 sidecar 本地的 AUTH_DISABLED 逃逸开关。生产环境务必在
# Django 进程环境中设置 TESTGEN_AUTH_TOKEN，并与 sidecar 的 AUTH_TOKEN 保持一致。
#
# 取令牌优先级（修复：项目使用 python-decouple 的 config() 读取 .env，但 decouple
# 不会把值写回 os.environ；若只查 os.environ 会恒为空，导致代理始终不注入令牌、
# sidecar 返回 401，testgen 控制台 API 全部不可用）。故优先读 Django settings
# （已由 decouple 填充），再回退 os.environ。
def _sidecar_token() -> str:
    return getattr(settings, "TESTGEN_AUTH_TOKEN", "") or os.environ.get(
        "TESTGEN_AUTH_TOKEN", ""
    )

# 不向下游透传的逐跳头/敏感头
_HOP_BY_HOP = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    # 控制台 API 前缀只能由本代理注入，客户端自带的一律丢弃（防止伪造前缀）
    "x-testhub-api-prefix",
}

# 平台为控制台分配的专属 API 前缀：注入到控制台 HTML，使其请求落到
# /testgen-api/v1/ 而不是与主平台共用的 /api/v1/
_CONSOLE_API_PREFIX = "/testgen-api/v1"

# ---------------------------------------------------------------------------
# 旧 `/api/v1/` 通道命中计数（B3 下线前的观察窗）
#
# 控制台已改走专属前缀 `/testgen-api/v1/`，旧 `/api/v1/` 兜底仅作兼容保留。
# 是否还有调用方依赖旧通道，只能靠实际流量判断：这里对每次命中累加计数，并按
# 「每累计 N 次」或「距上次输出满 5 分钟」输出一条汇总日志，避免每请求一条刷屏。
# 计数仅存于本进程内存，随进程重启归零——观察期内 grep 日志
# "旧 /api/v1/ 通道命中" 即可看到窗口增量与路径分布。
#
# 下线判据：连续 1~2 周日志中「本窗口 +N」恒为 0（只剩归属纠正的 404），
# 即可删除 backend/urls.py 的 `^api/v1/(?P<rest>.*)$` 路由，以及
# testgen_api_proxy 的 dedicated=False 分支与 _platform_path_hint 判定（B3）。
# ---------------------------------------------------------------------------
_LEGACY_FLUSH_EVERY = 50  # 每累计 N 次输出一条汇总
_LEGACY_FLUSH_INTERVAL = 300.0  # 或距上次输出满 N 秒也输出一条

_legacy_lock = threading.Lock()
_legacy_hits: dict[str, int] = {}  # 顶层路径段 → 累计命中次数
_legacy_total = 0
_legacy_misroute = 0
_legacy_flushed: dict[str, int] = {}  # 上次输出时的快照，用于算窗口增量
_legacy_flushed_total = 0
_legacy_last_flush = time.monotonic()


def _record_legacy_hit(rest: str, misroute: bool = False) -> None:
    """记录一次旧 `/api/v1/` 通道命中，必要时输出窗口汇总日志。

    线程安全（Django 多线程 / runserver 均可能并发调用）；日志在锁外发出，
    避免慢日志 handler 阻塞请求线程。
    """
    global _legacy_total, _legacy_misroute, _legacy_flushed_total, _legacy_last_flush

    top = rest.split("/", 1)[0] or "<root>"
    with _legacy_lock:
        _legacy_hits[top] = _legacy_hits.get(top, 0) + 1
        _legacy_total += 1
        if misroute:
            _legacy_misroute += 1
        now = time.monotonic()
        if (
            _legacy_total % _LEGACY_FLUSH_EVERY != 0
            and now - _legacy_last_flush < _LEGACY_FLUSH_INTERVAL
        ):
            return
        window = {k: v - _legacy_flushed.get(k, 0) for k, v in _legacy_hits.items()}
        total = _legacy_total
        misroute_total = _legacy_misroute
        delta = total - _legacy_flushed_total
        _legacy_flushed.update(_legacy_hits)
        _legacy_flushed_total = total
        _legacy_last_flush = now

    hot = sorted(((k, v) for k, v in window.items() if v), key=lambda kv: -kv[1])[:8]
    logger.info(
        "旧 /api/v1/ 通道命中：本窗口 +%d 次（累计 %d，其中归属纠正 %d）；"
        "按路径：%s。控制台已改走 %s，增量长期为 0 即可下线该兼容路由。",
        delta,
        total,
        misroute_total,
        ", ".join(f"{k}={v}" for k, v in hot) or "无",
        _CONSOLE_API_PREFIX,
    )


# ---------------------------------------------------------------------------
# 命名空间归属判定
#
# 背景：`/api/v1/` 这条兜底路由原本只为转发 sidecar 而存在（控制台 JS 把 API
# 路径写死成 `/api/v1/`），但它是**全前缀通配**，于是 `/api/v1/users/login/`
# 这类明显属于主平台的路径也会被它吃掉：匿名访问得到 403「未授权的代理访问」
# （语义误导，让人以为是鉴权问题），已登录访问则把请求体（含明文密码）转发给
# sidecar 并可能写进其日志。这里在转发前做归属判定，命中平台路径就直接 404 并
# 给出正确路径，既纠正语义也避免凭据外流。
# ---------------------------------------------------------------------------

# `/api/v1/<seg>` 中归属 sidecar 的顶层段（来源：services/testgen/service/app.py）。
# 注意 `projects` 与平台的 `/api/projects/` 同名但前缀不同，必须算作 sidecar 段，
# 否则控制台的「项目列表」会被误判为平台路径而 404。sidecar 新增与平台同名的顶层
# 路由时，需同步加到这里。
_SIDECAR_TOP_SEGMENTS = frozenset(
    {
        "analyze",
        "branches",
        "commits",
        "compare",
        "describe",
        "dialogue",
        "execute",
        "generate",
        "llm",
        "metrics",
        "parse-input",
        "pipeline",
        "projects",
        "pull",
        "repos",
        "scan",
        "tasks",
        "verdict",
        "verify",
        "workspaces",
    }
)

# `path('api/', include('apps.api_testing.urls'))` 无法从 route 反推子前缀，显式补齐。
# 注意其真实路径是 `/api/api-testing/...`，与下面的 f"/api/{rest}" 拼装结果一致。
_EXTRA_PLATFORM_SEGMENTS = frozenset({"api-testing"})

_platform_segments_cache: frozenset[str] | None = None


def _platform_top_segments() -> frozenset[str]:
    """平台自身的 `/api/<seg>/` 顶层段（惰性计算并缓存）。

    直接从已注册的 urlpatterns 推导，新增 app 自动跟随，无需手工维护清单。
    """
    global _platform_segments_cache
    if _platform_segments_cache is None:
        found = set(_EXTRA_PLATFORM_SEGMENTS)
        for pattern in get_resolver().url_patterns:
            route = getattr(getattr(pattern, "pattern", None), "_route", "") or ""
            matched = re.fullmatch(r"api/([^/<>]+)/", route)
            if matched:
                found.add(matched.group(1))
        _platform_segments_cache = frozenset(found)
    return _platform_segments_cache


def _platform_path_hint(rest: str) -> str | None:
    """`/api/v1/<rest>` 若其实指向主平台，返回正确路径；属 sidecar 则返回 None。"""
    top = rest.split("/", 1)[0]
    if not top or top in _SIDECAR_TOP_SEGMENTS:
        return None
    if top in _platform_top_segments():
        return f"/api/{rest}"
    return None


def _wrong_namespace_response(request, rest: str, hint: str) -> JsonResponse:
    """路径属于主平台却打在 `/api/v1/` 上：明确 404 + 正确路径，而不是 403/转发。"""
    logger.warning(
        "路径归属纠正：%s 属主平台接口，未转发 sidecar（建议 %s）", request.path, hint
    )
    return JsonResponse(
        {
            "error": "该路径不属于 testgen 服务",
            "path": request.path,
            "hint": f"平台接口请改用 {hint}",
            "detail": (
                "/api/v1/ 已被 testgen 控制台反向代理占用（同源嵌入 sidecar，其前端 JS "
                "写死该前缀）；主平台自身 API 不使用版本前缀。"
            ),
        },
        status=404,
    )


def _is_authenticated(request) -> bool:
    user = getattr(request, "user", None)
    return bool(user is not None and getattr(user, "is_authenticated", False))


def _do_proxy(
    request, target_path: str, extra_headers: dict | None = None
) -> HttpResponse:
    # M1：仅允许已登录用户经代理访问 sidecar，阻断匿名 CSRF / 未授权访问。
    if not _is_authenticated(request):
        return HttpResponseForbidden(
            "未授权的代理访问：/api/v1/ 与 /testgen/ 转发到 testgen sidecar，需先登录主平台"
        )

    target = f"{SIDECAR_BASE}/{target_path.lstrip('/')}"
    query = request.META.get("QUERY_STRING", "")
    if query:
        target += "?" + query

    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}

    # 代理侧显式注入（覆盖语义）：如控制台的专属 API 前缀
    if extra_headers:
        headers.update(extra_headers)

    # M1：显式注入服务端共享令牌，使 sidecar 的 H8 鉴权通过（以正确凭据代替
    # AUTH_DISABLED 本地逃逸）。始终覆盖请求中自带的 X-Auth-Token，避免被伪造。
    _token = _sidecar_token()
    if _token:
        headers["X-Auth-Token"] = _token

    body = request.body if request.method in ("POST", "PUT", "PATCH") else None

    try:
        resp = requests.request(
            method=request.method,
            url=target,
            headers=headers,
            data=body,
            # M2：不再把用户的 Django 会话 Cookie 透传给 sidecar（sidecar 走
            # X-Auth-Token 鉴权，无需也不应接收会话 Cookie），杜绝会话泄漏。
            timeout=120,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        return HttpResponse(
            f"testgen 代理错误: 无法连接 sidecar ({target}): {exc}", status=502
        )

    excluded = _HOP_BY_HOP | {"content-encoding", "content-length"}
    resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded}

    return HttpResponse(resp.content, status=resp.status_code, headers=resp_headers)


@xframe_options_exempt
def testgen_web_proxy(request, rest: str = ""):
    """转发 /testgen/... → sidecar 根。rest 为 /testgen/ 之后的路径。

    页面为只读 GET/HEAD，Django 默认对安全方法不校验 CSRF，故无需 @csrf_exempt。
    转发时注入 `X-Testhub-Api-Prefix`，让控制台把 API 请求打到专属前缀
    （`/testgen-api/v1`）而非与主平台共用的 `/api/v1/`。
    """
    # 控制台的静态资源（chart.umd.min.js）用**相对路径**引用，以便自动跟随代理前缀；
    # 相对路径要求页面 URL 以 `/` 结尾，故仅对「无尾斜杠的页面根」重定向一次。
    if not rest and not request.path.endswith("/"):
        query = request.META.get("QUERY_STRING", "")
        return redirect(f"{request.path}/{('?' + query) if query else ''}")
    return _do_proxy(
        request, rest, extra_headers={"X-Testhub-Api-Prefix": _CONSOLE_API_PREFIX}
    )


# M1 残留说明：testgen 控制台是从 sidecar 下发的“异源 JS”（非主平台 SPA），
# 其调用 /api/v1/ 的 POST/PUT/DELETE 不会携带 Django CSRF 令牌，若强制 CSRF 校验
# 会令控制台不可用，故此处保留 @csrf_exempt。补偿控制：① 已要求代理访问必须登录
# （阻断匿名 CSRF）；② 已注入服务端 X-Auth-Token（sidecar H8 鉴权）；③ 已去除会话
# Cookie 透传。后续建议：让控制台改用带 CSRF 令牌或 scoped token 的调用，方可移除
# 本 @csrf_exempt（彻底收敛 M1）。
@csrf_exempt
@xframe_options_exempt
def testgen_api_proxy(request, rest: str = "", dedicated: bool = False):
    """转发 `/api/v1/...` 或 `/testgen-api/v1/...` → sidecar 的 `/api/v1/...`。

    `dedicated=True` 对应平台分配的专属前缀（`/testgen-api/v1/`）：该命名空间只属于
    sidecar，无需归属判定，任何路径都应转发。

    `dedicated=False` 是旧 `/api/v1/` 兜底兼容：转发前先判归属，若该路径其实是主平台
    接口（如 `users/login/`），直接 404 并告知正确路径，既不误报 403，也不把平台请求体
    （含明文密码）转发给 sidecar。该分支每次命中都会记账（见 `_record_legacy_hit`），
    用于判断何时可以安全下线这条兼容路由。
    """
    if not dedicated:
        hint = _platform_path_hint(rest)
        _record_legacy_hit(rest, misroute=bool(hint))
        if hint:
            return _wrong_namespace_response(request, rest, hint)
    return _do_proxy(request, f"api/v1/{rest}")
