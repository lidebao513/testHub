"""受保护的静态文件服务（M3 整改）。

`django.views.static.serve` 自身**没有任何鉴权**，直接挂在 URL 上等于把
APP 自动化的模板目录与 Allure 报告目录对匿名用户完全开放：可枚举目录、
可下载历史测试产物（截图、日志、失败详情，常含被测系统内部信息）。

本模块在其前面加一道门：

1. **必须已登录**：浏览器 iframe 请求会自动带会话 Cookie（登录时
   `django.contrib.auth.login` 已写入 session），未登录一律拒绝；
2. **路径穿越/隐藏文件拒绝**：`..`、反斜杠、空字节、以 `.` 开头的文件
   直接 404，不交给下游；
3. **扩展名白名单**：只放行报告与静态资源所需的类型，避免把任意
   文件（如 `.py`、`.env`、`.sql`）当静态资源读取；
4. **禁止目录列表**：目录请求一律 404（`django.views.static.serve`
   本身不列表，这里显式兜底）；
5. 统一补 `X-Content-Type-Options: nosniff`，避免产物被浏览器
   猜成脚本执行。

生产建议：仍应由 Nginx `X-Accel-Redirect` 或对象存储签名 URL 接管，
把静态 I/O 从 Python 进程移出；本视图用于「不额外部署组件」的场景。
"""
from __future__ import annotations

import os
import posixpath

from django.http import HttpResponse, HttpResponseNotFound
from django.views.static import serve as _django_serve

# 报告与静态资源所需的扩展名（Allure 报告 + 模板截图）
ALLOWED_EXTENSIONS = frozenset(
    {
        'html', 'htm', 'js', 'mjs', 'css', 'json', 'txt', 'xml', 'csv',
        'map', 'svg', 'png', 'jpg', 'jpeg', 'gif', 'webp', 'ico', 'bmp',
        'woff', 'woff2', 'ttf', 'otf', 'eot',
    }
)

_UNSAFE_TEXT = '拒绝访问：路径不合法'


def _is_safe_rel_path(rel: str) -> bool:
    """相对路径安全检查：拒绝穿越、隐藏文件、绝对路径、空字节。"""
    if not rel or '\x00' in rel:
        return False
    rel = rel.replace('\\', '/')
    if rel.startswith('/'):
        return False
    parts = [p for p in rel.split('/') if p not in ('', '.')]
    if any(p == '..' for p in parts):
        return False
    # 隐藏文件/隐藏目录（.git/.env/..keep 等）一律不可访问
    if any(p.startswith('.') for p in parts):
        return False
    return bool(parts)


def _is_allowed_ext(rel: str) -> bool:
    ext = posixpath.splitext(rel)[1].lstrip('.').lower()
    return ext in ALLOWED_EXTENSIONS


def protected_serve(request, path: str, document_root: str | None = None, **kwargs):
    """登录 + 白名单校验后的静态文件服务。

    签名与 `django.views.static.serve` 一致，便于直接替换 URL 配置。
    """
    if not getattr(request.user, 'is_authenticated', False):
        return HttpResponse(
            '未登录或登录已过期，无法访问该资源',
            status=401,
            content_type='text/plain; charset=utf-8',
        )

    rel = (path or '').replace('\\', '/')
    if not _is_safe_rel_path(rel):
        return HttpResponseNotFound(_UNSAFE_TEXT)
    if not _is_allowed_ext(rel):
        return HttpResponseNotFound('拒绝访问：文件类型不在白名单内')

    root = document_root or kwargs.pop('document_root', None)
    if root:
        # 二次确认：规范化后必须仍在 document_root 之内
        target = os.path.normpath(os.path.join(root, *rel.split('/')))
        root_abs = os.path.normcase(os.path.abspath(root))
        target_abs = os.path.normcase(os.path.abspath(target))
        try:
            common = os.path.normcase(
                os.path.abspath(os.path.commonpath([target_abs, root_abs]))
            )
        except ValueError:
            return HttpResponseNotFound(_UNSAFE_TEXT)
        if common != root_abs:
            return HttpResponseNotFound(_UNSAFE_TEXT)
        if os.path.isdir(target):
            return HttpResponseNotFound('拒绝访问：不允许目录浏览')
        if not os.path.isfile(target):
            return HttpResponseNotFound('资源不存在')

    response = _django_serve(request, rel, document_root=root, **kwargs)
    response['X-Content-Type-Options'] = 'nosniff'
    return response
