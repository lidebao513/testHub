"""testgen 用例产物 → testhub TestCase 映射层（单向字段翻译，不搬运 testgen 代码）。

输入：testgen 用例 dict。来源可能是：
  - `GET /api/v1/projects/{pid}/cases` 返回的 DB 行（字段：tc_no/title/case_type/priority/
    precondition/doc_steps(Json文本)/steps(Json文本)/ctype/test_type(全量·更新)/tp_id/fp_contract_id/module）；
  - 或 `CaseSpec.to_dict()`（字段：tc_no/title/case_type/priority/precondition/doc_steps(list)/
    expect/steps(list)/ctype/...）。
  两类来源都能处理（doc_steps/steps 可能是 list 也可能是 Json 文本；DB 行无 expect 列）。

输出：testhub `apps.testcases.models.TestCase` 创建/更新 payload，字段名严格对齐真实模型。

与阶段文档的偏差（已按 testhub 真实模型修正，避免写库报错）：
  - testhub TestCase **没有** `case_type` / `external_id` / `source` / `suite` 字段；
  - testgen 维度(case_type: 正常/异常/安全/边界) → 映射到 testhub `test_type` 枚举；
  - testgen 优先级(P0-P3) → 映射到 testhub `priority` 枚举(low/medium/high/critical)；
  - 稳定指纹 `tc_no` 以标签 `tg:<tc_no>` 形式存入 `tags`，并作为幂等 upsert 键；
    来源以 `testgen` 标签标记；
  - `suite_id` 仅作追溯写入 `description`，不写入不存在的模型字段；
  - `author` 为必填外键，由调用方以 `author_id` 提供；
  - `steps` 字段 `max_length=1000`，超长截断，全文放入 `description`。
"""
from __future__ import annotations

import json
from typing import Any

# testgen 维度(case_type) → testhub 测试类型枚举（test_type 取值：functional/integration/
# api/ui/performance/security）。安全/性能直接映射；正常/异常/边界再按 ctype/layer 细化。
_DIM_TO_TEST_TYPE = {
    "安全": "security",
    "性能": "performance",
}

# testgen 优先级 P0-P3 → testhub 优先级枚举（low/medium/high/critical）
_PRIORITY_MAP = {
    "P0": "critical",
    "P1": "high",
    "P2": "medium",
    "P3": "low",
}

# 维度 → 标签
_DIM_TO_TAG = {
    "安全": "security",
    "性能": "performance",
    "边界": "boundary",
    "异常": "abnormal",
    "正常": "normal",
}

# 幂等 upsert 标签前缀：tg:<tc_no>
TESTGEN_ID_TAG_PREFIX = "tg:"


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, dict)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def render_doc_steps(doc_steps: Any) -> str:
    """把 testgen 的 doc_steps（list[dict] 或 Json 文本）渲染为人类可读步骤文本。"""
    if not doc_steps:
        return ""
    if isinstance(doc_steps, str):
        try:
            doc_steps = json.loads(doc_steps)
        except (TypeError, ValueError):
            return doc_steps  # 已是纯文本
    if not isinstance(doc_steps, list):
        return _as_text(doc_steps)
    lines: list[str] = []
    for i, step in enumerate(doc_steps, 1):
        if isinstance(step, dict):
            kind = step.get("kind") or ""
            action = step.get("action") or step.get("step") or ""
            expect = step.get("expect") or step.get("断言") or step.get("expected") or ""
            parts: list[str] = []
            if kind:
                parts.append(f"[{kind}]")
            if action:
                parts.append(_as_text(action))
            line = f"{i}. " + " ".join(parts) if parts else f"{i}."
            if expect:
                line += f" → 预期：{_as_text(expect)}"
            lines.append(line)
        else:
            lines.append(f"{i}. {_as_text(step)}")
    return "\n".join(lines)


def _extract_expected(cs: dict) -> str:
    """抽取预期结果：优先 expect 字段；否则 doc_steps/steps 末步预期；否则空。

    DB 行的 cases 表**没有 expect 列**，所以必须能从 doc_steps/steps 推导。
    """
    expect = cs.get("expect")
    if expect:
        return _as_text(expect)

    doc = cs.get("doc_steps")
    if isinstance(doc, str):
        try:
            doc = json.loads(doc)
        except (TypeError, ValueError):
            doc = None
    if isinstance(doc, list):
        for step in reversed(doc):
            if isinstance(step, dict):
                e = step.get("expect") or step.get("断言") or step.get("expected")
                if e:
                    return _as_text(e)

    steps = cs.get("steps")
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except (TypeError, ValueError):
            steps = None
    if isinstance(steps, list) and steps:
        first = steps[0]
        if isinstance(first, dict) and first.get("expect"):
            return _as_text(first["expect"])
    return ""


def _is_ui_layer(cs: dict) -> bool:
    """判断用例是否属 UI 层（用于 test_type 推断）。

    不直接对整条 cs 做文本匹配：DB 行/JSON 文本里的 doc_steps 中文常被
    json.dumps 转义为 \\uXXXX，整串匹配会失效。改为先解析 doc_steps 再判断。
    """
    ctype = (cs.get("ctype") or "").lower()
    if ctype == "ui":
        return True
    if ctype == "api":
        return False
    # 解析 doc_steps（可能是 list，或 JSON 文本）检测 UI 标记
    doc = cs.get("doc_steps")
    if isinstance(doc, str):
        try:
            doc = json.loads(doc)
        except (TypeError, ValueError):
            doc = None
    if isinstance(doc, list):
        for step in doc:
            if isinstance(step, dict):
                kind = str(step.get("kind") or "").lower()
                act = str(step.get("action") or "").lower()
                layer = str(step.get("layer") or "").lower()
                if "ui" in kind or "ui" in act or layer == "ui":
                    return True
                if "页面" in kind or "页面" in act or "交互" in kind or "交互" in act:
                    return True
    # 回退：低成本字符串标记检测（ctype/layer/ui_probe 等字段）
    blob = json.dumps(cs, ensure_ascii=False).lower()
    return any(m in blob for m in ("ui_probe", "layer\": \"ui\"", "kind\": \"ui\""))


def map_case_type_to_test_type(cs: dict) -> str:
    """testgen 维度(case_type) → testhub test_type 枚举。"""
    dim = (cs.get("case_type") or "").strip()
    if dim in _DIM_TO_TEST_TYPE:
        return _DIM_TO_TEST_TYPE[dim]
    ctype = (cs.get("ctype") or "").lower()
    if ctype == "api":
        return "api"
    if _is_ui_layer(cs):
        return "ui"
    return "functional"


def map_priority(priority: Any) -> str:
    """testgen 优先级 P0-P3 → testhub priority 枚举（默认 medium）。"""
    p = (str(priority) if priority is not None else "").strip().upper()
    return _PRIORITY_MAP.get(p, "medium")


def build_tags(cs: dict) -> list[str]:
    """构造 testhub tags：来源标记 + 维度标签 + 幂等指纹标签。"""
    tags = ["testgen"]
    dim = (cs.get("case_type") or "").strip()
    dim_tag = _DIM_TO_TAG.get(dim)
    if dim_tag:
        tags.append(dim_tag)
    tc_no = cs.get("tc_no") or cs.get("tp_id") or ""
    if tc_no:
        tags.append(f"{TESTGEN_ID_TAG_PREFIX}{tc_no}")
    return tags


def testgen_id_of(cs: dict) -> str:
    """稳定外部指纹：tc_no 优先，否则 tp_id。用于幂等 upsert 键。"""
    return (cs.get("tc_no") or cs.get("tp_id") or "").strip()


def case_spec_to_testcase(
    cs: dict,
    *,
    project_id: int,
    author_id: int,
    suite_id: int | None = None,
) -> dict:
    """testgen 用例 dict → testhub TestCase payload（字段对齐真实模型）。

    返回的键可直接用于 `TestCase.objects.update_or_create(...)` / `TestCase.objects.create(**payload)`：
    project_id / author_id 为外键按 id 赋值。
    """
    doc_steps_text = render_doc_steps(cs.get("doc_steps"))
    expected = _extract_expected(cs)
    full_steps_text = doc_steps_text or _as_text(cs.get("steps"))
    steps_text = full_steps_text
    if len(steps_text) > 1000:
        steps_text = steps_text[:997] + "…"

    trace: list[str] = []
    if suite_id is not None:
        trace.append(f"suite_id={suite_id}")
    if cs.get("tp_id"):
        trace.append(f"tp_id={cs['tp_id']}")
    if cs.get("fp_contract_id"):
        trace.append(f"fp_id={cs['fp_contract_id']}")
    if cs.get("module"):
        trace.append(f"module={cs['module']}")
    if cs.get("tc_no"):
        trace.append(f"testgen_tc_no={cs['tc_no']}")
    trace.append("source=testgen")

    description = (cs.get("description") or "")
    description = (description + "\n") if description else ""
    description += "追溯：" + "; ".join(trace)
    # steps 字段 max_length=1000，超长截断；完整步骤保留到 description（TextField 无限长）
    if len(full_steps_text) > 1000:
        description += "\n完整步骤：\n" + full_steps_text

    return {
        "project_id": int(project_id),
        "author_id": int(author_id),
        "title": (cs.get("title") or "").strip() or f"testgen {testgen_id_of(cs)}",
        "description": description.strip(),
        "preconditions": _as_text(cs.get("precondition")),
        "steps": steps_text,
        "expected_result": expected,
        "priority": map_priority(cs.get("priority")),
        "test_type": map_case_type_to_test_type(cs),
        "tags": build_tags(cs),
        "status": "active",
    }
