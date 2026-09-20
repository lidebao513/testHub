"""对话代理（Dialogue Agent）· 薄适配层。

职责
----
把「用户意图」翻译成 `DialogueTemplate.validate_and_parse` 能消费的 filled dict：

- ``handle_template(filled)``：模板优先路径，直接校验 filled 字典；
- ``handle_text(text)``：自由文本兜底路径，先用 `core.auto_input.parse_auto_input`
  （纯规则）切字段，再判定 source_kind，最后复用模板校验；
- **规则优先 → 可选 LLM → 关键字 → unknown 诚实降级**：LLM 默认关闭，未配置时
  直接走规则；规则也拿不准（判不出来源）就如实报 unknown，绝不静默猜测。

本模块**不触达**任何能力层（不调 pipeline / 不建请求），只产出
`DialoguePlan`（含 PipelineRequest 形状 params），由 CLI / HTTP 层负责执行。
"""

from __future__ import annotations

import os
import re
from typing import Any

from core.auto_input import parse_auto_input
from core.config import get_settings
from core.enums import MODE_CHOICES
from engine.dialogue.template import DialoguePlan, DialogueTemplate, default_dialogue_template


# LLM 增强为「可选」扩展点：默认关闭，避免在未配置 LLM 时产生额外调用与不可复现结果。
# 设为 "1" / "true" 且 settings.llm_enhance 与 api_key 就绪时，方启用意图精修。
_LLM_ENABLED = os.environ.get("DIALOGUE_LLM_ENABLED", "").lower() in ("1", "true", "yes")


_GIT_HOST_RE = re.compile(
    r"(github\.com|gitlab|gitee|bitbucket|code\.up|code\.aliyun|\.git$|^git@)",
    re.IGNORECASE,
)


def _is_git_url(u: str) -> bool:
    """判断一个 URL 是否是 Git 仓库地址（而非运行时被测环境地址）。"""
    return bool(u) and bool(_GIT_HOST_RE.search(u))


class DialogueAgent:
    """对话代理：模板 / 文本两条入口，统一产出 DialoguePlan。"""

    def __init__(self, template: DialogueTemplate | None = None) -> None:
        self.template = template or default_dialogue_template()

    # ---- 入口 1：模板（优先） ----
    def start(self) -> DialogueTemplate:
        """返回模板（CLI 用它渲染/打印，HTTP 用它序列化 schema）。"""
        return self.template

    def handle_template(self, filled: dict[str, Any]) -> DialoguePlan:
        """校验模板填写值，产出执行计划。"""
        return self.template.validate_and_parse(filled or {})

    # ---- 入口 2：自由文本（兜底） ----
    def handle_text(self, text: str) -> DialoguePlan:
        """把一段混排文本解析为执行计划（复用统一智能输入框规则）。"""
        parsed = parse_auto_input(text or "")
        filled = self._auto_input_to_filled(parsed, text)

        # 可选 LLM 精修（默认关闭，未配置时直接跳过 → 规则结果）
        if _LLM_ENABLED:
            refined = self._maybe_llm_refine(text, filled)
            if refined is not None:
                filled = refined

        plan = self.template.validate_and_parse(filled)
        plan.intent = "text"
        # 规则未能判定来源：诚实降级为 unknown
        if not filled.get("source_kind"):
            plan.intent = "unknown"
            plan.errors = [
                "无法从文本判定来源：请使用模板，或在文本中明确给出"
                "「仓库地址 / 本地代码目录 / 被测环境地址」之一"
            ]
            plan.params = {}
            plan.redacted = {}
        return plan

    # ---- 内部：AutoInputResult → filled ----
    def _auto_input_to_filled(self, parsed: Any, raw_text: str) -> dict[str, Any]:
        url = (parsed.url or "").strip()
        lp = (parsed.local_path or "").strip()

        # 关键校正：parse_auto_input 把「仓库 / 代码 / repo」别名的值收进 local_path，
        # 但该值可能是 Git 仓库地址（应进 repo_url）而非本地目录。统一按形态归位：
        # - Git 仓库地址 → repo_url（无论来自 url 还是 local_path）
        # - 本地已存在目录 → local_path
        # - 其余 Web 地址 → test_url
        test_url = ""
        repo_url = ""
        local_path = ""
        if url:
            if _is_git_url(url):
                repo_url = url
            else:
                test_url = url
        if lp:
            if _is_git_url(lp):
                repo_url = repo_url or lp
            else:
                local_path = lp

        login_user = (parsed.user or "").strip()
        login_password = parsed.password or ""
        login_otp = parsed.otp or ""

        has_code = bool(repo_url or local_path)
        has_url = bool(test_url)
        if has_code and has_url:
            source_kind = "code+url"
        elif has_code:
            source_kind = "code"
        elif has_url:
            source_kind = "url"
        else:
            source_kind = ""

        mode = (parsed.mode or "").strip()
        if mode not in MODE_CHOICES:
            mode = ""

        scopes = list(parsed.scopes or [])

        return {
            "source_kind": source_kind,
            "mode": mode,
            "scopes": scopes,
            "test_url": test_url,
            "repo_url": repo_url,
            "local_path": local_path,
            "login_user": login_user,
            "login_password": login_password,
            "login_otp": login_otp,
            "base": (parsed.base or "").strip() or "",
            "target": (parsed.target or "").strip() or "",
            "project_name": (parsed.project_name or "").strip(),
            # 原始文本原文回灌，作为安全网（显式字段已置位，apply_auto_input 不会覆盖）
            "auto_input": raw_text.strip(),
        }

    # ---- 内部：可选 LLM 意图精修（默认关闭） ----
    def _maybe_llm_refine(self, text: str, filled: dict[str, Any]) -> dict[str, Any] | None:
        """可选 LLM 精修：把文本再解析为结构化字段，合并进 filled。

        仅在 ``_LLM_ENABLED`` 且 settings 就绪时调用；任何异常都返回 None（诚实降级到规则）。
        """
        settings = get_settings()
        if not (settings.llm_enhance and settings.llm_api_key):
            return None
        try:
            from engine.llm_fallback import chat_with_fallback

            prompt = (
                "从用户文本中抽取测试用例生成参数，只输出 JSON："
                '{"source_kind":"code|url|code+url","mode":"full|incremental",'
                '"scopes":["正常","安全"],"test_url":"","repo_url":"","local_path":"",'
                '"login_user":"","base":"","target":"","project_name":""}。'
                f"用户文本：{text}"
            )
            resp = chat_with_fallback(
                channel="dialogue",
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                timeout=settings.llm_timeout,
                model=settings.llm_model,
                model_chain=list(settings.llm_model_chain or []),
                messages=[{"role": "user", "content": prompt}],
            )
            import json

            content = getattr(resp, "choices", [{}])[0].get("message", {}).get("content", "")
            data = json.loads(content)
            merged = dict(filled)
            for k, v in data.items():
                if v:
                    merged[k] = v
            return merged
        except Exception:  # noqa: BLE001
            return None
