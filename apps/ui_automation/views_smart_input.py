"""#184 统一智能输入框 — 解析预览端点（只解析、不执行）。"""
from __future__ import annotations

from typing import ClassVar

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ui_automation.smart_input import (
    compose_task_description,
    mask_password,
    parse_unified_input,
)


class SmartInputParseView(APIView):
    """POST /api/ui-automation/smart-input/parse/  body: {text}

    返回结构化解析结果；明文密码**不出接口**，仅回传 password_masked
    （明文本就在用户浏览器输入框里，执行时由前端随请求体送 run_adhoc）。
    """

    permission_classes: ClassVar[list] = [IsAuthenticated]

    def post(self, request):
        text = request.data.get('text') or ''
        if not str(text).strip():
            return Response({'error': '缺少 text 参数'}, status=400)
        parsed = parse_unified_input(str(text))
        parsed['password_masked'] = mask_password(parsed.get('password') or '')
        # 回填用：不含明文密码的任务描述（供「拆分回填」写回现有表单）
        parsed['task_description'] = compose_task_description(parsed, include_credentials=False)
        parsed.pop('password', None)
        parsed.pop('raw', None)  # raw 含原始明文密码，不出接口
        return Response({'ok': True, 'parsed': parsed})
