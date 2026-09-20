"""引擎层：从代码到用例的六个模块 + 编排。

依赖方向（由 `scripts/check_structure.py` 强制）：
    engine -> core            （引擎可用契约与基础设施）
    engine  x  service / cli  （引擎不感知 HTTP 与命令行）

模块边界（各自单一职责，互不反向依赖）：
    scan            目录 -> 文件集合（AST 只解析一次）
    fp_extract      文件 -> 功能点 FP
    diff_tag        git diff -> 变更集与 hunk 区间 -> 增量标签
    tp_expand       功能点 -> 测试点 TP（四维展开 + 范围过滤）
    semantic_enrich 规则增强 + 可选 LLM 增强 + 防胡说校验层
    case_gen        测试点 -> 用例 Case（八要素 + 确定性）
    pipeline        以上串联 + 幂等落库
"""

from __future__ import annotations


__all__ = [
    "case_gen",
    "diff_tag",
    "executor",
    "fp_extract",
    "llm_design",
    "pipeline",
    "prd_ingest",
    "runtime_ui",
    "scan",
    "semantic_enrich",
    "stage_registry",
    "tp_expand",
]
