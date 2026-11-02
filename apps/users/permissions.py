"""用户相关对象级权限。

H6 整改：修复任意已登录用户可查看/修改/删除他人账号、以及可枚举全部账号的问题。
通过对象级权限 + 列表作用域收敛，确保：
  - 普通用户只能访问/修改自己；
  - 列表仅返回自己（管理员可看全部）；
  - 管理员（is_staff）可访问任意用户对象。
"""

from rest_framework.permissions import BasePermission


class IsOwnerOrAdmin(BasePermission):
    """对象级权限：仅本人或管理员（is_staff）可访问/修改目标用户对象。"""

    message = "无权访问或修改该用户资源"

    def has_permission(self, request, view):
        # 列表/创建阶段只要求已登录；对象级校验在 has_object_permission 中完成。
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        return obj == request.user or request.user.is_staff
