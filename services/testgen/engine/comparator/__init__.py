"""comparator：LLM 语义比对器（模块化 P0 新增能力层）。

对外三种复用方式（见 docs/comparator_modularization_design.md）：
  ① import：from engine.comparator import compare_one
  ② CLI：testgen compare ...（P0-2 落地）
  ③ HTTP：POST /api/v1/compare（P0-2 落地）
"""

from .compare import (
    ComparatorOptions,
    CompareInput,
    CompareVerdict,
    compare_batch,
    compare_one,
    redact,
)


__all__ = [
    "ComparatorOptions",
    "CompareInput",
    "CompareVerdict",
    "compare_batch",
    "compare_one",
    "redact",
]
