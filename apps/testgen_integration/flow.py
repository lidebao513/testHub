"""回流编排：testgen 用例 → testhub TestCase（幂等 upsert，逻辑与 Django 解耦）。

把「调用 testgen → 映射 → 写库」的编排收口在此模块，``upsert`` 回调由调用方注入，
使得回流逻辑可在无 Django 环境下单测：

- 真实环境：``upsert(testgen_id, payload)`` 写入 ``apps.testcases.models.TestCase``，
  以 ``tg:<testgen_id>`` 标签做幂等 update_or_create；
- 单测环境：内存 dict 模拟 ``upsert``，断言幂等与计数。

幂等键：``testgen_id``（= tc_no，稳定内容指纹）。相同指纹重复回流 → 更新而非新建，
不会翻倍用例。
"""
from __future__ import annotations

from typing import Any, Callable

from .mapping import case_spec_to_testcase, testgen_id_of

# upsert 回调返回值约定
CREATED = "created"
UPDATED = "updated"
SKIPPED = "skipped"


def sync_cases(
    cases: list[dict[str, Any]],
    *,
    project_id: int,
    author_id: int,
    upsert: Callable[[str, dict[str, Any]], str],
    suite_id: int | None = None,
) -> dict[str, int]:
    """把 testgen 用例列表映射并回流到 testhub。

    参数：
      cases        testgen 用例 dict 列表
      project_id   testhub 项目 id
      author_id    testhub 用户 id（TestCase.author 必填）
      upsert       注入的写库回调，返回 CREATED/UPDATED/SKIPPED
      suite_id     可选，仅作追溯写入 description

    返回：{"total","created","updated","skipped"}
    """
    created = updated = skipped = 0
    for cs in cases:
        tg_id = testgen_id_of(cs)
        if not tg_id:
            # 无稳定指纹，无法幂等，跳过避免重复污染
            skipped += 1
            continue
        payload = case_spec_to_testcase(
            cs, project_id=project_id, author_id=author_id, suite_id=suite_id
        )
        result = upsert(tg_id, payload)
        if result == CREATED:
            created += 1
        elif result == UPDATED:
            updated += 1
        else:
            skipped += 1
    return {
        "total": len(cases),
        "created": created,
        "updated": updated,
        "skipped": skipped,
    }
