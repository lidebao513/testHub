"""专家系统 · 统一多模态 LLM 包装（engine/expert/llm）。

设计要点（继承既有红线）：
- **多模态**：消息体支持 `image_url`（截图）；纯文本模型（如 deepseek-chat）**不能读图**，
  此时 `use_vision` 为 True 但模型非视觉时，自动**剥离图片并显式 notes**（延续 F10b 不静默），
  绝不假装已读截图。
- **结构化输出**：强制 JSON 数组；失败只重试一次；再失败则抛错由调用方降级。
- **统一故障语义**：调用方统一拿不到结果时，返回「空 added + notes 说明」，不影响规则基线。

只依赖 core（契约/枚举/日志），不感知 service / cli。
OpenAI 依赖惰性导入（未安装则报错指明，不阻断导入）。
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any

from core.log import get_logger


log = get_logger(__name__)


# 已知「视觉模型」关键字：命中即认为该 endpoint 真能读图。
# 缺省 deepseek-chat / qwen 文本模型不在此列 → 即便 use_vision=True 也走 DOM 兜底。
_VISION_MODEL_HINTS = ("vision", "vl", "qwen-vl", "gpt-4o", "gpt-4-turbo", "claude", "gemini")


def is_vision_model(model: str) -> bool:
    """粗略判断模型名是否指向视觉模型（仅用于自动降级决策）。"""
    m = (model or "").lower()
    return any(h in m for h in _VISION_MODEL_HINTS)


@dataclass
class ExpertLLMOptions:
    """专家 LLM 选项（与既有 EnrichOptions 字段兼容，增 vision 维度）。"""

    enabled: bool = False
    provider: str = "deepseek"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    timeout: int = 300
    # D1：多模态开关；即使为 True，若模型非视觉（见 is_vision_model）也会自动降级 DOM 并 notes。
    use_vision: bool = True
    # 专用于读图的视觉模型名；为空则与 model 同值（由 is_vision_model 判定能否吃图）。
    vision_model: str = ""
    # 降级链：模型不可用（如免费额度 AllocationQuota.FreeTierOnly.）时按顺序切换的备选模型。
    # 委托 engine.llm_fallback.chat_with_fallback 统一处理，不在此处各自实现降级。
    model_chain: list[str] = field(default_factory=list)


@dataclass
class ExpertLLMClient:
    """OpenAI 兼容客户端（Qwen / DeepSeek / 视觉模型均可，仅换 base_url 与 model）。"""

    options: ExpertLLMOptions

    def available(self) -> bool:
        return bool(
            self.options.enabled
            and self.options.api_key
            and self.options.base_url
            and (self.options.model or self.options.model_chain)
        )

    def can_read_image(self) -> bool:
        """本配置下是否真的能读图：use_vision 且模型名命中视觉模型。"""
        if not self.options.use_vision:
            return False
        model = self.options.vision_model or self.options.model
        return is_vision_model(model)

    def complete_json(
        self,
        user_prompt: str,
        *,
        system_prompt: str = "",
        images: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """请求模型并解析 JSON 数组；返回 (数据, 元数据)。

        元数据含 `used_vision` / `vision_note`，调用方可据此在产物里诚实标注。
        图片仅在 `can_read_image()` 为真时带入；否则剥离并记入拒绝原因（不静默）。

        实际调用统一委托 `engine.llm_fallback.chat_with_fallback`（含降级链），
        本客户端不再各自实现降级逻辑。
        """
        from engine.llm_fallback import chat_with_fallback

        meta: dict[str, Any] = {"used_vision": False, "vision_note": ""}
        messages = self._build_messages(user_prompt, system_prompt, images, meta)
        resp = chat_with_fallback(
            channel="expert",
            api_key=self.options.api_key,
            base_url=self.options.base_url,
            timeout=self.options.timeout,
            model=self.options.model,
            model_chain=self.options.model_chain,
            messages=messages,
            temperature=0.2,
        )
        raw = resp.choices[0].message.content or "[]"
        return _parse_json_array(raw), meta

    def _build_messages(
        self,
        user_prompt: str,
        system_prompt: str,
        images: list[str] | None,
        meta: dict[str, Any],
    ) -> list[dict[str, Any]]:
        sys_text = system_prompt or _DEFAULT_SYSTEM
        content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
        if images:
            if self.can_read_image():
                for b64 in images:
                    content.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": (
                                    b64
                                    if b64.startswith("data:")
                                    else f"data:image/png;base64,{b64}"
                                )
                            },
                        }
                    )
                meta["used_vision"] = True
            else:
                meta["vision_note"] = (
                    "use_vision=True 但当前模型非视觉模型（%s），已降级为仅 DOM 文本输入，"
                    "未读取截图" % (self.options.vision_model or self.options.model)
                )
        messages: list[dict[str, Any]] = [{"role": "system", "content": sys_text}]
        if len(content) == 1:
            messages.append({"role": "user", "content": user_prompt})
        else:
            messages.append({"role": "user", "content": content})
        return messages


_DEFAULT_SYSTEM = (
    "你是资深软件测试专家。请基于给定事实（页面渲染 / 代码）补充测试视角。"
    "硬性约束：1) 只能引用确实存在的元素/路由/接口/XHR，不得臆造；"
    "2) 只输出 JSON 数组，不要解释文字。"
)


def _parse_json_array(raw: str) -> list[dict[str, Any]]:
    """解析模型返回的 JSON 数组；容错 ``` 代码块 / 前缀噪声 / 对象包裹。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = _re_strip_code(text)
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end <= start:
            return []
        try:
            data = json.loads(text[start : end + 1])
        except ValueError:
            return []
    if isinstance(data, dict):
        data = data.get("items", [])
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def _re_strip_code(text: str) -> str:
    import re

    return re.sub(r"^```[a-zA-Z]*\n?|```$", "", text).strip()


def encode_image(path_or_b64: str) -> str:
    """把图片文件读成 base64 串（供 use_vision 路径）；已是 data: 则返回原串。"""
    if path_or_b64.startswith("data:"):
        return path_or_b64
    with open(path_or_b64, "rb") as fh:
        return base64.b64encode(fh.read()).decode("ascii")
