"""对话代理模块（Dialogue Agent）· 薄适配层。

对外暴露：
- ``DialogueTemplate`` / ``default_dialogue_template``：模板 schema 与渲染；
- ``DialogueAgent``：模板 / 自由文本两条入口，产出 ``DialoguePlan``；
- ``DialoguePlan`` / ``TemplateField`` / ``VisibilityRule``：数据结构。
"""

from engine.dialogue.agent import DialogueAgent
from engine.dialogue.template import (
    DialoguePlan,
    DialogueTemplate,
    TemplateField,
    VisibilityRule,
    default_dialogue_template,
)


__all__ = [
    "DialogueAgent",
    "DialoguePlan",
    "DialogueTemplate",
    "TemplateField",
    "VisibilityRule",
    "default_dialogue_template",
]
