"""testgen-capabilities —— 存储无关的 testgen 能力层门面包。

设计红线（见 docs/testgen_modularization_task_list.md P2-1）：
    能力层**不读** testgen 的 projects / cases / runs 存储表。
本包只重新导出 testgen 中「纯能力」模块：

    - ``comparator``        ：LLM 语义比对（规则为主 · LLM 增强 · 诚实降级）
    - ``llm_fallback``      ：统一 LLM 降级链（多模型链 / 进程缓存 / 视觉兜底）
    - ``dialogue``          ：对话代理（模板优先 + 自由文本兜底，产出生成请求）

这些模块本身不依赖 DB / pipeline / 执行动作；跨项目调用方自带存储后端
（依赖注入）—— 例如 ``comparator.compare_one`` 完全由入参驱动，零存储。

安装（二选一）：
    1) 在 testgen-service 根目录执行：
         pip install -e packages/testgen-capabilities
    2) 或设置环境变量指向 testgen-service 根目录：
         set TESTGEN_SERVICE_ROOT=C:/.../work/testgen-service
       后再 ``import testgen_capabilities``，本包会自动把该根目录加入 sys.path。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


__all__ = [
    "ComparatorOptions",
    "CompareInput",
    "CompareVerdict",
    "DialogueAgent",
    "DialoguePlan",
    # dialogue
    "DialogueTemplate",
    # llm_fallback
    "chat_with_fallback",
    "compare_batch",
    # comparator
    "compare_one",
    "default_dialogue_template",
    "redact",
]


def _ensure_engine_importable() -> None:
    """让本包能找到 testgen-service 的 ``engine`` 包（存储无关的能力层）。

    优先信任 ``TESTGEN_SERVICE_ROOT`` 环境变量；否则从本文件向上回溯，
    寻找含 ``engine/__init__.py`` 的目录并加入 sys.path。
    """
    try:
        import engine  # noqa: F401  - 已可导入则直接返回

        return
    except Exception:  # noqa: BLE001 - 后续兜底加入路径
        pass

    candidates: list[Path] = []
    root = os.environ.get("TESTGEN_SERVICE_ROOT")
    if root:
        candidates.append(Path(root))
    here = Path(__file__).resolve()
    for depth in range(1, 8):
        parent = here
        for _ in range(depth):
            parent = parent.parent
        candidates.append(parent)

    for cand in candidates:
        if (cand / "engine" / "__init__.py").exists():
            resolved = str(cand)
            if resolved not in sys.path:
                sys.path.insert(0, resolved)
            return


_ensure_engine_importable()

from engine.comparator import (  # noqa: E402
    ComparatorOptions,
    CompareInput,
    CompareVerdict,
    compare_batch,
    compare_one,
    redact,
)
from engine.dialogue import (  # noqa: E402
    DialogueAgent,
    DialoguePlan,
    DialogueTemplate,
    default_dialogue_template,
)
from engine.llm_fallback import chat_with_fallback  # noqa: E402
