"""#185 阶段二：Playwright MCP 脚本生成后端入口。

端点 ``POST /ui-automation/scripts/generate``：

- 入参：``{project_id, target_url, instruction, language}``。
- 安全：``target_url`` 必须过 ``is_safe_target_url`` 最小硬拦截（云元数据/内网私有段）；
  启动命令绝不带 ``browser_run_code_unsafe``（由 pw_mcp_client 保证）。
- 行为：调用 ``pw_mcp_client.generate_script`` 拉起 MCP 录制最小流程并产出 Playwright 脚本。
- 产物为**预览**（不落库）；前端复核后由 ``test-scripts`` 创建接口以 ``status='draft'`` 落库
  （preview/confirm 两段式，AI 产物默认草稿态，需人工确认）。
- 凭据兜底：``instruction`` 若含密码等敏感串，回显前统一打码，绝不进入响应/落库内容。
"""

from __future__ import annotations

import logging
import re

from rest_framework import status as drf_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .pw_mcp_client import generate_script, is_safe_target_url

logger = logging.getLogger(__name__)

# 测试可 patch 此入口，避免真实拉起 node/chromium。
GENERATE_FN = generate_script

# 简单凭据打码：命中常见密码/令牌字段的值，做局部打码（preview/confirm 前置把关）
# 兼容中英文 key（password/pwd/密码/口令…）与 `:`/`=`/空格 分隔，值长度 ≥6 避免误伤普通词。
_SECRET_RE = re.compile(
    r"(?i)(password|passwd|pwd|密码|口令|token|secret|access[_-]?key|authorization)"
    r"\s*[:=]?\s*('|\")?([^\s'\"&]{6,})",
)


def _mask_secret_in_text(text: str) -> str:
    """把文本里疑似凭据的值打码（用于 instruction 回显兜底）。"""
    if not text:
        return text

    def _repl(m):
        quote = m.group(2) or ""
        value = m.group(3)
        if len(value) <= 2:
            masked = value
        else:
            masked = (
                value[:2] + "***" + value[-1] if len(value) > 4 else value[:1] + "***"
            )
        return f"{m.group(1)}:{quote}{masked}"

    return _SECRET_RE.sub(_repl, text)


class ScriptGenerateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.data or {}
        target_url = str(data.get("target_url") or "").strip()
        instruction = str(data.get("instruction") or "").strip()
        language = str(data.get("language") or "python").strip()
        project_id = data.get("project_id")

        if not target_url:
            return Response(
                {"error": "缺少 target_url 参数"},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )
        if not is_safe_target_url(target_url):
            return Response(
                {"error": "目标 URL 被安全策略拦截（云元数据/内网私有段）"},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )
        if language not in ("python", "javascript"):
            language = "python"
        if not project_id:
            return Response(
                {"error": "缺少 project_id 参数"},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )

        # 凭据防线一（生成前）：instruction 里的密码/令牌先打码，再交给引擎。
        # 否则引擎会把明文当作填充文本写进生成脚本的 fill(...)，进而被落库
        # （2026-11-04 真实栈 E2E 实测缺陷：instruction 已打码但 content 仍带明文）。
        safe_instruction = _mask_secret_in_text(instruction)
        if instruction and instruction != safe_instruction:
            logger.warning("ScriptGenerateView: instruction 含疑似凭据，生成前已打码")

        try:
            result = GENERATE_FN(
                target_url=target_url, instruction=safe_instruction, language=language
            )
        except Exception as exc:  # 生成失败明确报错，不静默（fail-open 但可见）
            logger.exception("Playwright MCP 脚本生成失败 target_url=%s", target_url)
            return Response(
                {"error": f"脚本生成失败：{exc}"},
                status=drf_status.HTTP_502_BAD_GATEWAY,
            )

        content = str(result.get("content") or "")
        # 凭据防线二（生成后兜底）：content 同样过打码，防止凭据从页面/指令等
        # 其它来源混入脚本正文。
        safe_content = _mask_secret_in_text(content)
        if content != safe_content:
            logger.warning("ScriptGenerateView: 生成脚本含疑似凭据，已打码")
        content = safe_content
        steps = result.get("steps") or []
        out_language = result.get("language") or language

        return Response(
            {
                "content": content,
                "language": out_language,
                "framework": "playwright",
                "steps": steps,
                "status": "draft",
                "source": "pw-mcp",
                "target_url": target_url,
                "instruction": safe_instruction,
            }
        )
