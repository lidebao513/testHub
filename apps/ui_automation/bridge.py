"""testgen→AICase 桥接真实接线（#199）。

把桥接编排 ``apps.testgen_integration.flow.bridge_aicases`` 的注入式 ``bridge`` 回调
落地为真实写库：以 ``source_ref=tg:<tc_no>`` 做幂等 ``update_or_create``，project 保持 null
（AICase.project 是 UiProject，与 testgen 回流的通用 Project 是两套模型，不强行绑定，
靠 source_ref 溯源即可，避免 FK 错配崩溃）。
"""
from __future__ import annotations

from typing import Any

from apps.ui_automation.models import AICase


def bridge_aicase_real(tg_id: str, payload: dict[str, Any]) -> str:
    """真实桥接：以 ``source_ref=tg_id`` 幂等建/更 AICase。

    返回 ``'created'`` / ``'updated'``（与 ``flow.CREATED`` / ``UPDATED`` 约定对齐）。
    ``payload`` 由 ``apps.testgen_integration.mapping.case_spec_to_aicase`` 产出，
    仅含 AICase 合法字段（name / task_description / description / created_by_id）。
    """
    _obj, created = AICase.objects.update_or_create(
        source_ref=tg_id,
        defaults=dict(payload),
    )
    return "created" if created else "updated"
