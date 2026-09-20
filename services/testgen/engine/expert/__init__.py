"""测试专家系统 · 引擎层专家模块（engine/expert）。

两通道异构专家：
- PageExpert（URL 通道）：审视页面**运行时渲染**（截图/DOM/元素/XHR）；
- CodeExpert（代码通道，Phase 2）：审视程序**静态语义**（函数体/AST/调用图）。

本包只依赖 core（契约/枚举/日志），不感知 service / cli，符合分层铁律。
"""

from engine.expert.base import (
    ExpertResult,
    validate_area_hits,
)
from engine.expert.llm import ExpertLLMClient, ExpertLLMOptions
from engine.expert.page_expert import (
    PageCapture,
    PageExpert,
    PageExpertOptions,
    PageExpertResult,
)


__all__ = [
    "ExpertLLMClient",
    "ExpertLLMOptions",
    "ExpertResult",
    "PageCapture",
    "PageExpert",
    "PageExpertOptions",
    "PageExpertResult",
    "validate_area_hits",
]
