"""G5：安全缺陷 ORM 直写。

把 ``flow.run_security_verdict`` 生成的缺陷 payload 直接写成 testhub ``Defect`` 记录，
绕过 testhub 自环 HTTP（比 ``client.create_security_defect`` 更稳，且无需 ``TESTHUB_BASE_URL``）。

payload（来自 ``flow.run_security_verdict`` 的 G5 已修正 payload）字段约定：
  title, description, severity, priority, defect_type, source,
  project_id, reporter_id, related_testcase_id(可选, FK 可空)

字段已按 ``Defect`` 模型对齐：必填 FK 一律用 ``<fk>_id`` 整数形式（Django 直接接受）。
``Defect.objects.create(**payload)`` 即可落库，无需翻译层。
"""

from __future__ import annotations

from typing import Any


def create_security_defect_orm(payload: dict[str, Any]):
    """把安全裁判产出的缺陷 payload 直写 ``Defect`` 表，返回新建实例。

    作为 ``create_defect`` 注入回调供 ``flow.run_security_verdict`` 调用；
    G3 端点与（未来）G4 执行钩子共用本函数。

    必填校验在 import 模型之前完成：缺失 ``project_id`` / ``reporter_id`` 时
    直接抛 ``ValueError``，且不会触发 Django 加载（便于离线单测）。
    """
    if not payload.get("project_id"):
        raise ValueError(
            "安全缺陷必须关联规范 apps.projects.Project 的 id（Defect.project 必填）；"
            "引擎用例（ui_automation.TestCase→UiProject / api_testing.ApiRequest→ApiProject）"
            "与 Defect.project 非同一张表、无关联字段，请走决策②：security 用例收敛为规范 "
            "apps.testcases.TestCase（其 project 即 apps.projects.Project，已验证可直落缺陷）"
        )
    if not payload.get("reporter_id"):
        raise ValueError("安全缺陷必须关联 reporter_id（Defect.reporter 必填）")

    # 延迟导入：避免模块加载即触发 Django（离线单测 / 纯函数场景无需 Django）
    from apps.defects.models import Defect

    # 直接 ORM 直写；<fk>_id 整数形式已被 Django 接受（related_testcase_id 可空）
    return Defect.objects.create(**payload)
